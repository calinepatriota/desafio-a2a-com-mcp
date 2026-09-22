"""Servidor MCP da Central de Salas (Hill Valley Tech).

Streamable HTTP, revisao 2026-07-28 da spec, construido sobre o SDK oficial
(`mcp` v2, classe `MCPServer`). Expoe tres tools e um resource. A tool de
reserva usa o MRTR de primeira classe do SDK: um resolver anotado com
`Resolve(...)` devolve `Elicit(...)` quando o intervalo esta ocupado, e o
framework transforma isso num resultado `input_required` com `requestState`
protegido por integridade (`RequestStateSecurity`).

Nao ha canal de volta: o servidor termina a resposta pedindo a escolha, e o
cliente retorna num request novo levando `inputResponses` + `requestState`.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, create_model

from mcp.server.context import CallNext, HandlerResult, ServerRequestContext
from mcp.server.mcpserver import (
    AcceptedElicitation,
    CancelledElicitation,
    Context,
    DeclinedElicitation,
    Elicit,
    ElicitationResult,
    MCPServer,
    RequestStateSecurity,
    Resolve,
)
from mcp.server.mcpserver.exceptions import ToolError

# --------------------------------------------------------------------------- #
# Dominio: carregado dos arquivos JSON do starter (nao ha ORM nem banco).
# --------------------------------------------------------------------------- #

DADOS = Path(__file__).resolve().parents[1] / "dados"
SP = timezone(timedelta(hours=-3))  # America/Sao_Paulo, horario fixo do desafio
JANELA_INICIO = time(8, 0)
JANELA_FIM = time(20, 0)
DURACAO_MAXIMA = timedelta(hours=2)

# Mensagens: fonte unica de verdade (o validador exige o texto exato).
ERRO_SALA = "Sala inexistente: {sala}"
ERRO_JANELA = "Fora da janela de uso: a politica permite reservas entre 08:00 e 20:00"
ERRO_DURACAO = "Duracao acima do limite: a politica permite no maximo 2 horas"
ERRO_INTERVALO = "Intervalo invalido: fim deve ser posterior a inicio"
ERRO_SEM_ALTERNATIVA = "Sem alternativas disponiveis no intervalo"


def _carregar_json(nome: str) -> Any:
    import json

    return json.loads((DADOS / nome).read_text(encoding="utf-8"))


SALAS: list[dict[str, Any]] = _carregar_json("salas.json")
SALAS_POR_ID: dict[str, dict[str, Any]] = {s["id"]: s for s in SALAS}
# Copia mutavel em memoria: reservas criadas ficam visiveis nas consultas
# seguintes do mesmo processo, mas nao sobrevivem a um restart (por design).
RESERVAS: list[dict[str, Any]] = list(_carregar_json("reservas.json"))
POLITICA_TEXTO = (DADOS / "politica-de-uso.md").read_text(encoding="utf-8")
# A primeira linha declara a versao: "versao: 2026-11-01".
POLITICA_VERSAO = POLITICA_TEXTO.splitlines()[0].split(":", 1)[1].strip()

_contador = len(RESERVAS)


def _novo_id() -> str:
    global _contador
    _contador += 1
    return f"res-{_contador:04d}"


# --------------------------------------------------------------------------- #
# Validacao e regras (compartilhadas por consultar_disponibilidade e reservar).
# --------------------------------------------------------------------------- #


def _parse(iso: str) -> datetime:
    return datetime.fromisoformat(iso).astimezone(SP)


def _validar(sala: str, inicio: str, fim: str) -> tuple[datetime, datetime]:
    """Aplica sala + politica. Levanta ToolError com a mensagem exata do enunciado.

    Ordem: existencia da sala -> janela -> intervalo -> duracao.
    """
    if sala not in SALAS_POR_ID:
        raise ToolError(ERRO_SALA.format(sala=sala))
    ini, f = _parse(inicio), _parse(fim)
    if ini.timetz().replace(tzinfo=None) < JANELA_INICIO or f.timetz().replace(tzinfo=None) > JANELA_FIM:
        raise ToolError(ERRO_JANELA)
    if f <= ini:
        raise ToolError(ERRO_INTERVALO)
    if f - ini > DURACAO_MAXIMA:
        raise ToolError(ERRO_DURACAO)
    return ini, f


def _conflitos(sala: str, ini: datetime, fim: datetime) -> list[dict[str, Any]]:
    saida = []
    for r in RESERVAS:
        if r["sala"] != sala:
            continue
        if _parse(r["inicio"]) < fim and _parse(r["fim"]) > ini:
            saida.append(r)
    return saida


def _alternativas(sala: str, ini: datetime, fim: datetime) -> list[str]:
    """Salas livres no intervalo com capacidade >= a da pedida, no maximo tres,
    ordenadas por capacidade crescente e, em empate, por id alfabetico."""
    minima = SALAS_POR_ID[sala]["capacidade"]
    livres = [
        s
        for s in SALAS
        if s["id"] != sala and s["capacidade"] >= minima and not _conflitos(s["id"], ini, fim)
    ]
    livres.sort(key=lambda s: (s["capacidade"], s["id"]))
    return [s["id"] for s in livres[:3]]


# --------------------------------------------------------------------------- #
# Modelos de saida (Pydantic -> structuredContent + outputSchema declarado).
# --------------------------------------------------------------------------- #


class SalaOut(BaseModel):
    id: str
    nome: str
    capacidade: int
    recursos: list[str]


class ListaDeSalas(BaseModel):
    salas: list[SalaOut]


class ConflitoOut(BaseModel):
    id: str
    inicio: str
    fim: str
    responsavel: str


class Disponibilidade(BaseModel):
    sala: str
    livre: bool
    conflitos: list[ConflitoOut]


class ReservaOut(BaseModel):
    reserva: str | None = None
    reservado: bool = True
    sala: str | None = None
    inicio: str | None = None
    fim: str | None = None
    responsavel: str | None = None
    politica: str | None = None
    motivo: str | None = None


class SalaEscolhida(BaseModel):
    """Tipo estatico da resposta da elicitation (o enum concreto e dinamico)."""

    sala: str


# --------------------------------------------------------------------------- #
# Servidor.
# --------------------------------------------------------------------------- #


class LogPorRequest:
    """Middleware que registra no stderr o metodo, o id e o traceparent de cada
    request recebido (M3 aula 6: stderr no lugar do logging depreciado)."""

    async def __call__(self, ctx: ServerRequestContext[Any, Any], call_next: CallNext) -> HandlerResult:
        meta = (ctx.params or {}).get("_meta") or {}
        traceparent = meta.get("traceparent")
        print(
            f"[mcp] method={ctx.method} id={ctx.request_id} traceparent={traceparent}",
            file=sys.stderr,
            flush=True,
        )
        return await call_next(ctx)


def _segredo() -> bytes:
    valor = os.environ.get("REQUEST_STATE_SECRET")
    if not valor:
        print(
            "ERRO: defina REQUEST_STATE_SECRET (>=32 bytes). Gere com:\n"
            '  python3 -c "import secrets; print(secrets.token_hex(32))"',
            file=sys.stderr,
        )
        raise SystemExit(1)
    return valor.encode()


server: MCPServer = MCPServer(
    name="central-de-salas",
    version="1.0.0",
    # Chave vem do ambiente, nunca do codigo. TTL de 10 min (entre 5 e 30).
    # keys=[...] (nao ephemeral) para que um requestState continue valido apos
    # um restart do processo: o estado viaja no token, nao na memoria.
    request_state_security=RequestStateSecurity(keys=[_segredo()], ttl=600.0),
    middleware=[LogPorRequest()],
)


@server.tool(description="Lista todas as salas com capacidade e recursos.")
def listar_salas() -> ListaDeSalas:
    return ListaDeSalas(salas=[SalaOut(**s) for s in SALAS])


@server.tool(description="Diz se uma sala esta livre no intervalo, e quais reservas conflitam.")
def consultar_disponibilidade(sala: str, inicio: str, fim: str) -> Disponibilidade:
    ini, f = _validar(sala, inicio, fim)
    conflitos = _conflitos(sala, ini, f)
    return Disponibilidade(
        sala=sala,
        livre=not conflitos,
        conflitos=[
            ConflitoOut(id=c["id"], inicio=c["inicio"], fim=c["fim"], responsavel=c["responsavel"])
            for c in conflitos
        ],
    )


def escolha_de_sala(sala: str, inicio: str, fim: str) -> Elicit[SalaEscolhida] | SalaEscolhida:
    """Resolver do parametro `escolha` de reservar_sala.

    Roda antes do corpo da tool. Se a sala pedida esta livre, devolve-a. Se
    esta ocupada, devolve `Elicit(...)` restringindo a escolha as alternativas;
    o framework transforma isso em input_required. Se nao ha alternativa, e um
    erro de execucao da tool.
    """
    ini, f = _validar(sala, inicio, fim)
    if not _conflitos(sala, ini, f):
        return SalaEscolhida(sala=sala)
    alternativas = _alternativas(sala, ini, f)
    if not alternativas:
        raise ToolError(ERRO_SEM_ALTERNATIVA)
    esquema = create_model(
        "EscolhaDeSala",
        sala=(
            Literal[tuple(alternativas)],  # type: ignore[valid-type]
            Field(description="Sala alternativa escolhida"),
        ),
    )
    return Elicit("A sala pedida esta ocupada nesse intervalo. Escolha uma alternativa.", esquema)


@server.tool(description="Reserva uma sala. Se o intervalo estiver ocupado, pergunta qual alternativa usar.")
def reservar_sala(
    sala: str,
    inicio: str,
    fim: str,
    responsavel: str,
    escolha: Annotated[ElicitationResult[SalaEscolhida], Resolve(escolha_de_sala)],
) -> ReservaOut:
    if isinstance(escolha, (DeclinedElicitation, CancelledElicitation)):
        return ReservaOut(reservado=False, motivo="recusado")
    sala_final = escolha.data.sala
    rid = _novo_id()
    RESERVAS.append({"id": rid, "sala": sala_final, "inicio": inicio, "fim": fim, "responsavel": responsavel})
    return ReservaOut(
        reserva=rid,
        reservado=True,
        sala=sala_final,
        inicio=inicio,
        fim=fim,
        responsavel=responsavel,
        politica=POLITICA_VERSAO,
    )


@server.resource("politica://uso", mime_type="text/markdown", description="Politica de uso das salas.")
def politica_de_uso() -> str:
    return POLITICA_TEXTO


def main() -> None:
    host = os.environ.get("MCP_HOST", "127.0.0.1")
    port = int(os.environ.get("MCP_PORT", "7301"))
    print(f"[mcp] central-de-salas ouvindo em http://{host}:{port}/mcp", file=sys.stderr, flush=True)
    server.run(
        transport="streamable-http",
        host=host,
        port=port,
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
    )


if __name__ == "__main__":
    main()
