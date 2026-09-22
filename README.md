# A Ponte — agente A2A com MCP por dentro

Central de Salas da Hill Valley Tech: um **servidor MCP** (Streamable HTTP,
revisão 2026-07-28 da spec) expõe o domínio de reservas, e um **agente** o
consome por dentro (host MCP) enquanto se oferece ao mundo por fora (servidor
A2A v1.0). São dois processos separados que só se falam por HTTP.

- `servidor-mcp/servidor.py` — servidor MCP: 3 tools, 1 resource, os dois tipos
  de erro e o ciclo completo de MRTR na tool de reserva. Construído sobre o SDK
  oficial `mcp` v2 (`MCPServer`).
- `agente/agente.py` — host MCP (cliente oficial `mcp.client.Client`) + servidor
  A2A (Starlette). É onde a ponte MRTR ↔ Task acontece.

## Como rodar

A partir de um clone limpo do fork (Python 3.10+):

```bash
# 1. Ambiente + dependências travadas (SDK oficial do MCP v2)
./setup.sh
#    ou, manualmente:
#    python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt

# 2. Gere e exporte o segredo de integridade do requestState UMA vez.
#    (>= 32 bytes de aleatoriedade; nunca versione o valor — o repositório é público.)
export REQUEST_STATE_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
```

Suba os dois processos, cada um em seu terminal (deixe o stderr do servidor MCP
visível):

```bash
# Terminal 1 — servidor MCP na porta 7301. Precisa do REQUEST_STATE_SECRET exportado.
./mcp.sh
#    equivale a: source .venv/bin/activate && python3 servidor-mcp/servidor.py

# Terminal 2 — agente (A2A + host MCP) na porta 7300.
./agente.sh
#    equivale a: source .venv/bin/activate && python3 agente/agente.py
```

Rode o validador (com os dois processos **recém-iniciados** — reservas criadas
por uma execução mudam o resultado da seguinte):

```bash
source .venv/bin/activate
python3 validador/validar.py --agente http://localhost:7300 --mcp http://localhost:7301
```

> Se for reiniciar o servidor MCP durante os testes (ex.: para conferir que o
> `requestState` sobrevive a um restart), **exporte o mesmo `REQUEST_STATE_SECRET`**
> no terminal do MCP: o estado viaja assinado no token, e a chave precisa ser a
> mesma para o token continuar válido.

Portas e caminhos são configuráveis por variável de ambiente (`MCP_HOST`,
`MCP_PORT`, `AGENTE_HOST`, `AGENTE_PORT`, `AGENTE_URL`, `MCP_URL`), com os
padrões que o validador assume (`:7301/mcp` e `:7300/a2a` + card no well-known).

## Onde a ponte acontece

A ponte é a costura entre o `requestState` do MRTR (MCP) e a `Task` do A2A, e
mora em `agente/agente.py`:

- **IDA — `input_required` do MCP vira `TASK_STATE_INPUT_REQUIRED`**: em
  `_iniciar_reserva`, quando `client.session.call_tool(..., allow_input_required=True)`
  devolve um `InputRequiredResult`, o agente extrai o `enum` das alternativas e
  a chave do `inputRequests`, **guarda o `requestState` opaco em `task.pausa`**
  (nunca exposto ao cliente), e coloca a Task em `TASK_STATE_INPUT_REQUIRED` com
  a linha exata `alternativas: <ids>` (`task.dizer(...)`). Usa-se
  `allow_input_required=True` de propósito: sem isso, o cliente do SDK
  responderia a elicitation sozinho e a Task nunca pausaria.
- **VOLTA — o `requestState` retorna ao servidor**: em `_continuar`, ao chegar um
  `SendMessage` de continuação (`escolha=...`) referenciando a mesma Task, o
  agente **repete o `tools/call` original** — mesmos `arguments` (exigência do
  binding de integridade), `inputResponses` com a **mesma chave** e o
  `requestState` **ecoado sem modificação** — via um novo `call_tool` (o SDK
  cunha um id de JSON-RPC novo a cada chamada). `escolha=recusar` vira
  `action="decline"` e a Task termina em `TASK_STATE_CANCELED`.

O agente não implementa nenhuma regra de sala: conflito, política e alternativas
são decisão do servidor MCP. O `requestState` é tratado como opaco — guardado e
ecoado, nunca aberto nem reconstruído.

## Decisões técnicas

- **Proteção do `requestState`**: delegada ao SDK via
  `RequestStateSecurity(keys=[REQUEST_STATE_SECRET], ttl=600.0)` no
  `servidor-mcp/servidor.py`. O `RequestStateBoundary` sela cada resultado
  `input_required` com **AES-256-GCM** (chave derivada por HKDF-SHA256) e
  reverifica todo token recebido antes de qualquer handler: expiração, binding à
  requisição (método + **digest dos `arguments`**) e audiência (o nome do
  servidor). Consequências diretas: um token adulterado falha a verificação e é
  rejeitado com **-32602**; e como o envelope é amarrado ao digest dos
  `arguments`, um retry com argumentos adulterados também é rejeitado com -32602
  — os valores adulterados nunca tomam efeito.
- **Validade**: **10 minutos** (`ttl=600.0`, dentro da faixa de 5–30 min).
- **Chave**: vem de `REQUEST_STATE_SECRET` (≥32 bytes), **nunca** do código.
  Gere com `python3 -c "import secrets; print(secrets.token_hex(32))"`. Como a
  chave vem do ambiente e o estado viaja no token (não na memória do servidor),
  um retry funciona mesmo depois de reiniciar o processo do servidor MCP.
- **Estado das Tasks**: em memória no agente, no dicionário `TASKS` (`taskId → Task`).
  Cada `Task` guarda `id`, `contextId`, estado, histórico, artifacts e — quando
  pausada — o `requestState` opaco em `task.pausa` (por Task, jamais serializado
  numa resposta A2A). Reservas ficam em memória no servidor MCP (`RESERVAS`);
  ambos os estados não sobrevivem a um restart, por design — a exceção é o
  `requestState`, que sobrevive por ser selado com a chave do ambiente.
- **Sem LLM**: o agente decide por regra a partir de um pedido em formato fixo;
  não há dependência de SDK de provedor de LLM (veja `pyproject.toml` /
  `requirements.txt`). Dado o mesmo pedido, o resultado é sempre o mesmo.
- **traceparent**: o agente extrai o `trace-id` do header `traceparent` da
  chamada A2A e o propaga (mesmo trace-id, span-id novo) no `_meta.traceparent`
  de todos os requests MCP daquela Task; o servidor registra método, id e
  traceparent de cada request no stderr.

## Saída do validador

Execução com os dois processos recém-iniciados:

```
trace-id desta execucao: b9c1a7dc22f422241a01f5fd91c00ba9
procure esse valor no stderr do servidor MCP para conferir a propagacao do traceparent.

PASS 01 tools/list traz as tres tools
PASS 02 toda tool tem inputSchema de objeto
PASS 03 listar_salas devolve structuredContent e o mesmo JSON em texto
PASS 04 _meta sem protocolVersion devolve -32602 e HTTP 400
PASS 05 _meta sem clientCapabilities devolve -32602 e HTTP 400
PASS 06 tool inexistente e recusada, por -32602 ou por isError
PASS 07 resources/read de politica://uso devolve a politica
PASS 08 resources/read de URI inexistente devolve -32602
PASS 09 sala inexistente devolve isError com a mensagem exata
PASS 10 fora da janela devolve isError com a mensagem exata
PASS 11 duracao acima de 2h devolve isError com a mensagem exata
PASS 12 intervalo invertido devolve isError com a mensagem exata
PASS 13 conflito devolve input_required com inputRequests e requestState
PASS 14 a elicitation e form mode e oferece as alternativas na ordem certa
PASS 15 conflito sem a capability elicitation devolve -32021 e HTTP 400
PASS 16 retry com inputResponses e requestState conclui a reserva
PASS 17 requestState adulterado e rejeitado com -32602
PASS 18 argumentos adulterados no retry nao tomam efeito
PASS 19 recusa conclui sem reservar e sem isError
PASS 20 conflito sem alternativa possivel devolve isError com a mensagem exata

PASS 21 agent card responde 200 no well-known com JSON
PASS 22 o card declara a interface JSON-RPC com url e versao 1.0
PASS 23 o card declara a skill reservar-sala
PASS 24 SendMessage com sala livre conclui a Task
PASS 25 o artifact chama reserva e traz a versao da politica
PASS 26 GetTask devolve id, contextId e estado corrente
PASS 27 SendMessage com sala ocupada pausa a Task
PASS 28 a Task pausada lista as alternativas na ordem certa
PASS 29 escolha fora do enum mantem a Task pausada
PASS 30 a continuacao conclui a Task na sala escolhida
PASS 31 SendMessage em Task terminal e recusado
PASS 32 a recusa termina a Task em CANCELED
PASS 33 duas Tasks pausadas ao mesmo tempo concluem cada uma com a sua reserva
PASS 34 nenhuma resposta A2A carrega o requestState
PASS 35 sala inexistente termina a Task em FAILED com a mensagem da tool
PASS 36 o agente e deterministico: o mesmo pedido produz a mesma pausa

resumo: 36 passaram, 0 falharam, de 36 verificacoes
```
