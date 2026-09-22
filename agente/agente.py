"""Agente da Central de Salas: host MCP por dentro, servidor A2A por fora.

- Por fora (porta 7300): servidor A2A v1.0, binding JSON-RPC. Publica o Agent
  Card no well-known URI e atende SendMessage / GetTask com uma maquina de
  estados de Task.
- Por dentro: host MCP. Usa o cliente oficial do SDK (`mcp.client.Client`) para
  descobrir as tools do servidor MCP (tools/list), ler o resource da politica e
  chamar reservar_sala. NAO importa nenhuma funcao do servidor: fala HTTP.

A PONTE (o coracao do desafio) esta em `_iniciar_reserva` e `_continuar`:
  - IDA: quando o tools/call devolve `InputRequiredResult`, a Task vai para
    TASK_STATE_INPUT_REQUIRED, a linha "alternativas: ..." e devolvida ao
    cliente A2A e o `requestState` opaco fica guardado, ligado aquela Task.
  - VOLTA: quando chega um SendMessage de continuacao (mesmo taskId, texto
    "escolha=..."), o agente repete o tools/call ORIGINAL com um id de JSON-RPC
    novo, levando `inputResponses` (mesma chave) e o `requestState` ecoado sem
    modificacao. O requestState e opaco: guardado e ecoado, nunca aberto.

O agente nao decide regra de sala nenhuma (conflito, politica, alternativas sao
do servidor MCP). Ele traduz protocolo e carrega estado nomeado atraves da
fronteira. Nao ha LLM no caminho: o pedido chega em formato fixo e a decisao e
por regra, deterministica.
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import sys
from typing import Any

import anyio
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from mcp.client import Client
from mcp_types import ElicitResult, Implementation, InputRequiredResult

MCP_URL = os.environ.get("MCP_URL", "http://127.0.0.1:7301/mcp")
AGENTE_HOST = os.environ.get("AGENTE_HOST", "127.0.0.1")
AGENTE_PORT = int(os.environ.get("AGENTE_PORT", "7300"))
# URL publica anunciada no card (o avaliador usa localhost:7300).
AGENTE_URL = os.environ.get("AGENTE_URL", f"http://localhost:{AGENTE_PORT}")

TERMINAIS = {"TASK_STATE_COMPLETED", "TASK_STATE_CANCELED", "TASK_STATE_FAILED"}


# --------------------------------------------------------------------------- #
# Identificadores e traceparent.
# --------------------------------------------------------------------------- #


def _id(prefixo: str) -> str:
    return f"{prefixo}-{secrets.token_hex(6)}"


def _trace_id_de(traceparent: str | None) -> str | None:
    """Extrai o trace-id de um header traceparent "00-<trace>-<span>-<flags>"."""
    if not traceparent:
        return None
    partes = traceparent.split("-")
    return partes[1] if len(partes) >= 3 and partes[1] else None


def _meta_mcp(trace_id: str | None) -> dict[str, Any] | None:
    """Monta o _meta de um request MCP: propaga o MESMO trace-id (span novo)."""
    if not trace_id:
        return None
    return {"traceparent": f"00-{trace_id}-{secrets.token_hex(8)}-01"}


# --------------------------------------------------------------------------- #
# Estado das Tasks (em memoria). O requestState fica AQUI, nunca numa resposta.
# --------------------------------------------------------------------------- #


class Task:
    def __init__(self) -> None:
        self.id = _id("task")
        self.context_id = _id("ctx")
        self.state = "TASK_STATE_SUBMITTED"
        self.status_message: dict[str, Any] | None = None
        self.history: list[dict[str, Any]] = []
        self.artifacts: list[dict[str, Any]] = []
        # Estado interrompido do MRTR, por Task. Nunca serializado para o cliente.
        self.pausa: dict[str, Any] | None = None
        self.trace_id: str | None = None

    def registrar_usuario(self, mensagem: dict[str, Any]) -> None:
        self.history.append(mensagem)

    def _mensagem_agente(self, texto: str) -> dict[str, Any]:
        return {
            "messageId": _id("msg"),
            "role": "ROLE_AGENT",
            "parts": [{"text": texto}],
            "taskId": self.id,
            "contextId": self.context_id,
        }

    def dizer(self, estado: str, texto: str) -> None:
        self.state = estado
        self.status_message = self._mensagem_agente(texto)
        self.history.append(self.status_message)

    def para_dict(self) -> dict[str, Any]:
        status: dict[str, Any] = {"state": self.state}
        if self.status_message is not None:
            status["message"] = self.status_message
        return {
            "id": self.id,
            "contextId": self.context_id,
            "status": status,
            "history": self.history,
            "artifacts": self.artifacts,
        }


TASKS: dict[str, Task] = {}


# --------------------------------------------------------------------------- #
# Parsing do formato fixo (sem linguagem natural, sem LLM).
# --------------------------------------------------------------------------- #


def _campos(texto: str) -> dict[str, str]:
    campos: dict[str, str] = {}
    for token in texto.split():
        if "=" in token:
            chave, valor = token.split("=", 1)
            campos[chave] = valor
    return campos


# --------------------------------------------------------------------------- #
# Host MCP: descoberta e chamadas.
# --------------------------------------------------------------------------- #


async def _descobrir(app: Starlette, trace_id: str | None) -> None:
    """tools/list + leitura do resource da politica, uma vez (em runtime, nao
    hardcoded). Garante que o tools/list acontece antes do primeiro tools/call."""
    if app.state.tools is not None:
        return
    cliente: Client = app.state.mcp
    resultado = await cliente.list_tools(meta=_meta_mcp(trace_id))
    app.state.tools = {t.name for t in resultado.tools}
    if "reservar_sala" not in app.state.tools:
        raise RuntimeError(f"servidor MCP nao expoe reservar_sala; tools={sorted(app.state.tools)}")
    politica = await cliente.read_resource("politica://uso", meta=_meta_mcp(trace_id))
    texto = politica.contents[0].text
    app.state.politica = texto.splitlines()[0].split(":", 1)[1].strip()


def _artifact(sc: dict[str, Any], politica: str) -> dict[str, Any]:
    conteudo = {
        "reserva": sc.get("reserva"),
        "sala": sc.get("sala"),
        "inicio": sc.get("inicio"),
        "fim": sc.get("fim"),
        "responsavel": sc.get("responsavel"),
        "politica": politica,  # versao lida do resource politica://uso
    }
    return {"artifactId": _id("art"), "name": "reserva", "parts": [{"text": json.dumps(conteudo, ensure_ascii=False)}]}


# --------------------------------------------------------------------------- #
# A PONTE.
# --------------------------------------------------------------------------- #


async def _iniciar_reserva(app: Starlette, task: Task, texto: str) -> None:
    campos = _campos(texto)
    argumentos = {
        "sala": campos.get("sala", ""),
        "inicio": campos.get("inicio", ""),
        "fim": campos.get("fim", ""),
        "responsavel": campos.get("responsavel", ""),
    }
    cliente: Client = app.state.mcp
    async with app.state.lock:
        await _descobrir(app, task.trace_id)
        task.state = "TASK_STATE_WORKING"
        resultado = await cliente.session.call_tool(
            "reservar_sala", argumentos, meta=_meta_mcp(task.trace_id), allow_input_required=True
        )

    if isinstance(resultado, InputRequiredResult):
        chave = next(iter(resultado.input_requests))
        pedido = resultado.input_requests[chave]
        esquema = pedido.params.requested_schema
        campo = (esquema.get("properties") or {}).get("sala", {})
        enum = campo.get("enum") or ([campo["const"]] if "const" in campo else [])
        # IDA da ponte: input_required -> TASK_STATE_INPUT_REQUIRED, guardando o
        # requestState opaco ligado a esta Task (sem expor ao cliente A2A).
        task.pausa = {
            "request_state": resultado.request_state,
            "chave": chave,
            "argumentos": argumentos,
            "enum": list(enum),
        }
        # Linha exata (byte a byte), sem prefixo, na ordem do enum.
        task.dizer("TASK_STATE_INPUT_REQUIRED", "alternativas: " + ", ".join(enum))
        return

    _concluir(app, task, resultado)


async def _continuar(app: Starlette, task: Task, texto: str) -> None:
    campos = _campos(texto)
    escolha = campos.get("escolha", "")
    pausa = task.pausa or {}
    chave = pausa.get("chave")
    cliente: Client = app.state.mcp

    if escolha == "recusar":
        # recusa -> action decline; a Task termina cancelada.
        async with app.state.lock:
            await cliente.session.call_tool(
                "reservar_sala",
                pausa["argumentos"],
                input_responses={chave: ElicitResult(action="decline")},
                request_state=pausa["request_state"],
                meta=_meta_mcp(task.trace_id),
                allow_input_required=True,
            )
        task.pausa = None
        task.dizer("TASK_STATE_CANCELED", "Reserva cancelada: alternativas recusadas.")
        return

    if escolha not in pausa.get("enum", []):
        # Escolha fora do enum: mantem a Task pausada e repete as alternativas.
        task.dizer("TASK_STATE_INPUT_REQUIRED", "alternativas: " + ", ".join(pausa.get("enum", [])))
        return

    # VOLTA da ponte: repete o tools/call ORIGINAL (mesmos argumentos, exigido
    # pelo binding de integridade do requestState) com um id de JSON-RPC novo
    # (o SDK cunha um a cada call_tool), levando inputResponses com a mesma chave
    # e o requestState ecoado sem modificacao.
    async with app.state.lock:
        resultado = await cliente.session.call_tool(
            "reservar_sala",
            pausa["argumentos"],
            input_responses={chave: ElicitResult(action="accept", content={"sala": escolha})},
            request_state=pausa["request_state"],
            meta=_meta_mcp(task.trace_id),
            allow_input_required=True,
        )
    task.pausa = None
    _concluir(app, task, resultado)


def _concluir(app: Starlette, task: Task, resultado: Any) -> None:
    """Mapeia o resultado 'complete' do tools/call para o estado terminal da Task."""
    if getattr(resultado, "is_error", False):
        # Erro de execucao da tool -> Task FAILED com a mensagem EXATA da tool.
        texto = " ".join(p.text for p in resultado.content if getattr(p, "text", None))
        task.dizer("TASK_STATE_FAILED", texto)
        return
    sc = resultado.structured_content or {}
    if not sc.get("reservado", False):
        # recusa (nao deveria chegar aqui pelo caminho A2A, mas trata por seguranca).
        task.dizer("TASK_STATE_CANCELED", "Reserva nao realizada.")
        return
    task.artifacts = [_artifact(sc, app.state.politica)]
    task.dizer("TASK_STATE_COMPLETED", f"Reserva {sc.get('reserva')} confirmada na {sc.get('sala')}.")


# --------------------------------------------------------------------------- #
# Metodos A2A.
# --------------------------------------------------------------------------- #


async def _send_message(app: Starlette, params: dict[str, Any], traceparent: str | None) -> dict[str, Any]:
    mensagem = params.get("message") or {}
    texto = " ".join(p.get("text", "") for p in mensagem.get("parts", []))
    task_id = mensagem.get("taskId")

    if task_id:
        task = TASKS.get(task_id)
        if task is None:
            raise _RpcError(-32001, f"Task desconhecida: {task_id}")
        if task.state in TERMINAIS:
            raise _RpcError(-32002, f"Task em estado terminal ({task.state}): nao aceita novas mensagens")
        # trace-id da Task: usa o da requisicao atual quando presente.
        task.trace_id = _trace_id_de(traceparent) or task.trace_id
        task.registrar_usuario(mensagem)
        await _continuar(app, task, texto)
        return {"task": task.para_dict()}

    # Nova Task.
    task = Task()
    task.trace_id = _trace_id_de(traceparent)
    TASKS[task.id] = task
    task.registrar_usuario(mensagem)
    await _iniciar_reserva(app, task, texto)
    return {"task": task.para_dict()}


async def _get_task(app: Starlette, params: dict[str, Any]) -> dict[str, Any]:
    task = TASKS.get(params.get("id"))
    if task is None:
        raise _RpcError(-32001, f"Task desconhecida: {params.get('id')}")
    return {"task": task.para_dict()}


class _RpcError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# --------------------------------------------------------------------------- #
# Transporte HTTP (Starlette): well-known card + endpoint JSON-RPC /a2a.
# --------------------------------------------------------------------------- #


def _agent_card() -> dict[str, Any]:
    return {
        "name": "Central de Salas",
        "description": "Reserva salas de reuniao da Hill Valley Tech.",
        "provider": {"organization": "Hill Valley Tech", "url": "https://hillvalley.example"},
        "version": "1.0.0",
        "supportedInterfaces": [
            {"url": f"{AGENTE_URL}/a2a", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}
        ],
        "capabilities": {"streaming": False, "pushNotifications": False, "extendedAgentCard": False},
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": [
            {
                "id": "reservar-sala",
                "name": "Reservar sala",
                "description": "Reserva uma sala em um intervalo. Se houver conflito, pergunta qual alternativa usar.",
                "tags": ["salas", "agenda"],
                "inputModes": ["text/plain"],
                "outputModes": ["text/plain"],
                "examples": [
                    "reservar sala=sala-garagem inicio=2026-11-03T14:00:00-03:00 "
                    "fim=2026-11-03T15:00:00-03:00 responsavel=Marty"
                ],
            }
        ],
    }


async def agent_card(request: Request) -> JSONResponse:
    return JSONResponse(_agent_card())


async def a2a(request: Request) -> JSONResponse:
    try:
        corpo = await request.json()
    except Exception:
        return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
    rid = corpo.get("id")
    metodo = corpo.get("method")
    params = corpo.get("params") or {}
    traceparent = request.headers.get("traceparent")
    try:
        if metodo == "SendMessage":
            resultado = await _send_message(request.app, params, traceparent)
        elif metodo == "GetTask":
            resultado = await _get_task(request.app, params)
        else:
            raise _RpcError(-32601, f"Metodo desconhecido: {metodo}")
        return JSONResponse({"jsonrpc": "2.0", "id": rid, "result": resultado})
    except _RpcError as e:
        return JSONResponse({"jsonrpc": "2.0", "id": rid, "error": {"code": e.code, "message": e.message}})
    except Exception as e:  # pragma: no cover
        return JSONResponse({"jsonrpc": "2.0", "id": rid, "error": {"code": -32603, "message": f"{type(e).__name__}: {e}"}})


@contextlib.asynccontextmanager
async def lifespan(app: Starlette):
    async def _elicit_declara(context: Any, params: Any) -> Any:  # nunca chamado (allow_input_required=True)
        from mcp_types import ErrorData, INVALID_REQUEST

        return ErrorData(code=INVALID_REQUEST, message="o agente nao responde elicitation por conta propria")

    cliente = Client(
        MCP_URL,
        mode="2026-07-28",  # adota a revisao direto, sem handshake
        client_info=Implementation(name="agente-central-de-salas", version="1.0.0"),
        elicitation_callback=_elicit_declara,  # declara elicitation.form nas capabilities
        cache=None,
    )
    async with cliente:
        app.state.mcp = cliente
        app.state.tools = None
        app.state.politica = None
        app.state.lock = anyio.Lock()
        print(f"[agente] host MCP conectado a {MCP_URL}", file=sys.stderr, flush=True)
        yield


app = Starlette(
    routes=[
        Route("/.well-known/agent-card.json", agent_card, methods=["GET"]),
        Route("/a2a", a2a, methods=["POST"]),
    ],
    lifespan=lifespan,
)


def main() -> None:
    print(f"[agente] A2A ouvindo em {AGENTE_URL} (card em /.well-known/agent-card.json)", file=sys.stderr, flush=True)
    uvicorn.run(app, host=AGENTE_HOST, port=AGENTE_PORT, log_level="warning")


if __name__ == "__main__":
    main()
