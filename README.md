# Análise de Crédito - Entes Públicos

App web interno para o setor de licitações: digita o município, o sistema coleta dados
públicos oficiais, calcula um scorecard de crédito e gera um dossiê com recomendação
(verde/amarelo/vermelho) para decidir sobre pedidos grandes de entes públicos.

![Análise de Crédito - Entes Públicos](docs/assets/app.png)

## Fontes consultadas automaticamente

| Fonte | O que traz |
|---|---|
| **SICONFI — registro de entes** | **Nome, UF, população e CNPJ oficial de cada um dos 5.570 municípios; resolve o CNPJ da prefeitura automaticamente** |
| **SICONFI — RGF Anexo 05 / RREO Anexo 07** | **Disponibilidade de caixa, obrigações financeiras, restos a pagar processados e não processados (inscritos, pagos, cancelados), despesas liquidadas x pagas — com evolução de 3 anos.** Tolera a modalidade **Simplificada/semestral** dos municípios pequenos (ver abaixo). |
| Tesouro Nacional — CAPAG | Nota oficial de capacidade de pagamento (A/B/C/D) e os 3 indicadores |
| SICONFI (RGF/RREO) | RCL, despesa com pessoal, dívida consolidada líquida (% da RCL), resultado orçamentário |
| Portal da Transparência — sanções | CEIS, CNEP e CEPIM do CNPJ do órgão |
| Portal da Transparência — repasses | Histórico mensal de recursos federais recebidos (24 meses), por órgão repassador |
| Portal da Transparência — convênios | Convênios com a União, valores e situação de adimplência |
| PNCP | Contratos dos últimos 24 meses, inclusive compras anteriores de climatização |
| BrasilAPI | Situação cadastral do CNPJ do órgão |
| IBGE | População, PIB municipal e per capita |

As três consultas ao Portal da Transparência exigem a chave gratuita; sem ela o sistema
funciona e marca essas seções como pendentes.

Serasa e CAUC não têm API pública — o formulário tem campos manuais para colar o
resultado dessas consultas, que entram no dossiê.

## Instalação (Windows)

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
copy .env.example .env
# edite o .env e preencha as chaves (abaixo)
```

### Chaves necessárias

1. **`OPENAI_API_KEY`** — gera o texto do dossiê. Crie em https://platform.openai.com/api-keys
2. **`TRANSPARENCIA_API_KEY`** (opcional, recomendado) — consulta de sanções, repasses e
   convênios. Cadastro gratuito em https://portaldatransparencia.gov.br/api-de-dados/cadastrar-email
   (a chave chega por e-mail). Sem ela, o sistema funciona e marca essas seções como pendentes.

### Trocar de provedor de IA

A redação do dossiê é a única parte do sistema que usa IA, isolada em
[app/dossier.py](app/dossier.py). Para usar a Anthropic no lugar da OpenAI, basta no `.env`:

```
LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=...
```

O modelo padrão de cada provedor é configurável (`OPENAI_MODEL`, `ANTHROPIC_MODEL`).
Para ver quais modelos a sua chave OpenAI acessa: `.\.venv\Scripts\python listar_modelos.py`

## Uso

```powershell
.\.venv\Scripts\uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Abra http://127.0.0.1:8000 — digite o município, selecione na lista e clique em **Gerar dossiê**.
A análise leva de 1 a 5 minutos.

O **CNPJ da prefeitura é resolvido automaticamente** pelo registro do Tesouro, não digitado.
Isso não é só conveniência: um CNPJ digitado errado não falha, apenas traz dados de outra
entidade — e a análise cobre apenas municípios (não universidades ou autarquias).

A página do dossiê é dividida em duas colunas:

- **Esquerda** — a análise: anel do score, sub-scores por bloco, indicadores, medidores de limite
  legal, scorecard auditável e todas as evidências coletadas.
- **Direita** — duas abas: o **dossiê redigido pela IA** e um **chat** para perguntar sobre a
  análise. O chat responde com base no dossiê, no scorecard e nos dados coletados, e declara
  quando algo não está nos dados em vez de inventar.

O histórico do chat fica **salvo no banco**, não no navegador: sobrevive a um refresh, e as
perguntas de um analista ficam visíveis para o outro na mesma análise (com o nome de quem
perguntou, quando há login). O servidor mantém o histórico — o navegador envia apenas a
pergunta nova.

Outros detalhes:

- **Histórico**: todas as análises ficam salvas em `data/analises.db` (SQLite).
- **PDF**: no dossiê, use "Salvar em PDF / Imprimir" — a coluna do chat é omitida e o dossiê
  entra na sequência da análise.
- Caches locais em `data/`: base CAPAG (7 dias) e registro de municípios (30 dias).

## Como o score é calculado

Todo o cálculo é determinístico e auditável em [app/scoring.py](app/scoring.py) — a IA
não participa do cálculo, apenas redige o dossiê a partir dos números prontos:

| Dimensão | Peso |
|---|---|
| **Liquidez para fornecedores** (caixa ÷ obrigações financeiras) | 20 |
| **Pagamento de restos a pagar** (6 pago + 3 cancelado + 3 tendência) | 12 |
| **Execução de pagamento** (liquidado x pago) | 8 |
| **Tendência da liquidez** (3 anos, limitada pelo histórico) | 5 |
| Nota CAPAG | 30 |
| Despesa com pessoal (% RCL, limite LRF 54%) | 10 |
| Dívida consolidada líquida (% RCL, limite 120%) | 10 |
| Convênios federais (adimplência) | 8 |

| Sanções CEIS/CNEP/CEPIM | 8 |
| Porte populacional | 4 |
| PIB per capita | 3 |

O bloco de **comportamento de pagamento** (as quatro primeiras linhas, 45 pontos) pesa mais
que a CAPAG (30). A razão: a CAPAG responde *"este ente pode tomar dívida nova com garantia
da União?"*, enquanto a pergunta de um fornecedor é *"este ente paga a minha nota fiscal?"* —
relacionadas, mas não iguais. Os valores de caixa usam **recursos não vinculados**, o dinheiro
sem carimbo; o caixa vinculado a saúde, educação e FUNDEB infla o total mas não pode pagar
fornecedor comum.

O score (0–100) é normalizado pelas dimensões com dado disponível; fontes indisponíveis
viram "lacunas" declaradas e não penalizam o score. Regras adicionais: CAPAG D força
vermelho; CAPAG C, sanções ativas, caixa líquido negativo ou cobertura de dados < 50%
impedem o verde.

### Modalidade Simplificada dos municípios pequenos

Municípios com menos de 50 mil habitantes podem entregar os relatórios fiscais na forma
**Simplificada**: o RGF é semestral (`co_tipo_demonstrativo = "RGF Simplificado"`, periodicidade
`S`) e o RREO usa `"RREO Simplificado"`. A estrutura de contas é idêntica à completa — mesmos
`cod_conta` e anexos —, então os cálculos funcionam igual. O coletor tenta a modalidade completa
primeiro (a mais comum) e cai para a simplificada; a lógica fica em
[siconfi_base.py](app/collectors/siconfi_base.py). Sem isso, entes pequenos apareciam com 45% de
cobertura e o bloco de pagamento ausente, como se não tivessem entregado nada — quando na verdade
tinham entregado na outra modalidade.

### Linguagem de risco

O sistema **mede e classifica** o risco de crédito; a IA **explica** os fatores e sugere
mitigadores; a **decisão comercial** de aceitar ou não a exposição é sempre do setor de licitações.
Por isso a IA nunca escreve "aprovar" ou "não aprovar" — fala em classificação de risco e cautelas.

A classificação é derivada do semáforo no código ([scoring.py](app/scoring.py), campo
`classificacao_risco`), não improvisada pela IA, para ficar consistente entre execuções. Como a
faixa amarela é larga (score 45–69), ela é subdividida no ponto médio (57):

| Semáforo | Score | Classificação |
|---|---|---|
| Verde | ≥70 | risco favorável |
| Amarelo alto | 57–69 | risco moderado |
| Amarelo baixo | 45–56 | risco moderado a elevado |
| Vermelho | <45 | risco elevado |
| Qualquer, não confiável | — | risco não avaliável |

O semáforo oficial não muda — só o rótulo textual fica mais fino, para que um amarelo 68 e um
amarelo 46 não recebam a mesma leitura. O rótulo aparece como pill ao lado do semáforo e abre o
resumo executivo do dossiê.

### Confiabilidade do score

Um score alto sobre poucos dados passa falsa segurança. Quando os demonstrativos fiscais não são
localizados (nem na modalidade simplificada), o bloco de comportamento de pagamento — o mais
pesado — fica ausente, e o score passa a vir só de indicadores periféricos. Nesse caso o score é
marcado como **não confiável**: o semáforo não sobe além de amarelo, o anel do score aparece
desbotado, um aviso destacado explica a limitação e a IA abre o resumo por ela. A redação diz que
os dados "não foram localizados nas consultas ao SICONFI", não que o município "não entregou" — a
distinção importa, porque só o extrato oficial de entregas autorizaria a segunda afirmação.

### Restrições e ressalvas

São dois mecanismos distintos:

- **Restrição** rebaixa o semáforo (caixa livre negativo, sanções ativas, CAPAG C ou D,
  inadimplência recente em convênios, pendências no CAUC, cobertura de dados abaixo de 50%).
- **Ressalva** acompanha o semáforo sem rebaixá-lo — sinaliza o que merece atenção mesmo com o
  ente aprovado. Um município pode sair **verde com atenção**.

A ressalva atual é a **margem de caixa sobre a RCL**: o caixa livre dividido pela receita corrente
líquida, em faixas de ≥5% (folga forte), 2–5% (confortável), 1–2% (moderada), 0–1% (apertada) e
abaixo de zero (negativa, que já é restrição). Ela complementa o índice de cobertura: cobertura
responde *"as obrigações estão cobertas?"*, margem responde *"a folga é relevante para o porte da
operação?"*. O Rio de Janeiro cobre suas obrigações em 1,20x com R$ 295 milhões de folga — que são
0,8% de uma RCL de R$ 37 bilhões, uma margem fina. Não pontua, para não contar duas vezes a mesma
disponibilidade já avaliada em "Liquidez para fornecedores".

### Critérios que exigem trajetória, não só o último dado

Três dimensões olham a série histórica porque o nível isolado engana:

- **Restos a pagar** — 6 pontos pelo % pago, 3 pelo % cancelado (cancelar limpa o balanço sem
  quitar o compromisso) e 3 pela trajetória. A tendência compara **apenas exercícios fechados**:
  restos a pagar são quitados ao longo do ano, então um corte no 3º bimestre mostra menos pago
  que um fechamento, e comparar os dois seria comparar grandezas diferentes.
- **Tendência da liquidez** — a direção é medida por variação absoluta (variação percentual sobre
  base negativa inverte o sinal e classifica melhora como piora). O histórico limita a nota: um
  ente que passou 2 dos 3 anos sem cobrir as obrigações tem teto de 3/5 mesmo tendo melhorado —
  é recuperação recente, não solidez consolidada.
- **Convênios inadimplentes** — a contagem acumulada **não** bloqueia sozinha: um município grande
  acumula pendências antigas por idade, não por conduta. O bloqueio exige inadimplência recente
  (3 ou mais nos últimos 5 anos) ou valor material (1% ou mais da RCL). Dois cuidados na consulta:
  o filtro de situação é aplicado no servidor (paginar tudo e filtrar localmente truncava a
  contagem), e os resultados são filtrados **pelo CNPJ da prefeitura** — `codigoIBGE` na API
  significa "convenente localizado neste município", não "o convenente é este município", e sem
  esse filtro entram convênios de ONGs, fundações e empresas sediadas na cidade (no Rio de Janeiro,
  190 convênios de terceiros seriam atribuídos à prefeitura).
- **CAUC** — é a fonte autoritativa sobre impedimento de transferências da União e não tem API
  pública. O formulário tem um campo estruturado (não consultado / sem pendências / com
  pendências); pendências informadas aqui impedem o verde.

### O que é exibido mas não pontua, e por quê

O dossiê tem uma tabela dedicada a isso, para deixar o critério explícito:

- **Resultado orçamentário** — compara receitas realizadas com despesas liquidadas num corte
  parcial do exercício. As receitas entram distribuídas ao longo do ano e as despesas se
  concentram no segundo semestre, então saldo positivo em junho é o padrão esperado, não
  evidência de superávit. Só é conclusivo no fechamento.
- **Contratos do PNCP** — inteligência comercial (quem já vendeu, por quanto, quando). Não é
  indicador de crédito: contrato assinado não é pagamento efetuado, a classificação por texto
  do objeto é aproximada e o volume vem amostrado. O serviço do PNCP é instável (a mesma
  consulta responde em 2s ou estoura 504 em 70s, sem relação com o tamanho da página), então
  uma página que falha não aborta a coleta: fica o que já veio, e o dossiê avisa que a coleta
  saiu incompleta em vez de apresentar o número parcial como se fosse o total.
- **Repasses federais** — fluxo naturalmente irregular entre meses; qualquer faixa fixa
  produziria um indicador falso. Útil para ler a regularidade do caixa.

## Arquitetura

Em produção o app roda **serverless na AWS**, sem servidor ligado 24/7 — paga-se só pelo uso:

- **API Gateway (HTTP API)** — porta de entrada pública; encaminha as requisições ao Lambda.
- **AWS Lambda** — roda o app inteiro (FastAPI adaptado com [Mangum](https://mangum.io/)). O
  mesmo Lambda tem **dois papéis**: atende as requisições HTTP do navegador e, quando recebe um
  evento assíncrono, vira o *worker* que executa a análise pesada (~3 min) em segundo plano.
- **DynamoDB** — armazena análises, conversas do chat e o estado dos *jobs*.

A análise é **assíncrona**: `POST /analisar` cria um job e dispara o processamento em segundo
plano (o Lambda invoca a si mesmo); a página faz *polling* em `/status/{id}` e abre o dossiê
quando fica pronto. Assim cada requisição HTTP é curta (o API Gateway corta em 29s) e não há
espera travada de minutos.

O **armazenamento é escolhido automaticamente** em [app/config.py](app/config.py): SQLite em
disco no uso local; DynamoDB quando roda no Lambda (detectado por `AWS_LAMBDA_FUNCTION_NAME`).

## Deploy na AWS

Pré-requisitos: AWS CLI configurado; uma tabela **DynamoDB** (chaves `pk`/`sk`, modo on-demand);
uma função **Lambda** (python3.12) com um **API Gateway HTTP API** na frente; e as chaves do
`.env` cadastradas como variáveis de ambiente da função. O empacotamento usa
`requirements-lambda.txt` (sem uvicorn, com Mangum; `boto3` já vem no runtime do Lambda) e baixa
wheels Linux — roda a partir do Windows, sem Docker.

Para publicar uma nova versão do código:

```powershell
powershell -ExecutionPolicy Bypass -File deploy\deploy-lambda.ps1
```

Ajuste `AWS_PROFILE`, `AWS_REGION` e `FUNCTION_NAME` por variável de ambiente se os padrões não
baterem com a sua conta.

## Rodar em rede local (alternativa, sem AWS)

Para uso interno numa LAN, suba o uvicorn escutando na rede e **defina uma senha**:

```powershell
.\.venv\Scripts\uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Sem `APP_SENHA` o app não exige login — só aceitável em `127.0.0.1`. Exposto na rede sem senha,
qualquer máquina abriria os dossiês e dispararia análises, que consomem crédito de IA.

## Backup

- **Local (SQLite):** todo o estado (análises, dossiês e chats) está em `data/analises.db` —
  copiar esse arquivo é o backup completo. Os demais arquivos de `data/` são cache e se refazem.
- **Produção (DynamoDB):** habilite *Point-in-time recovery* na tabela para backup contínuo.

## Utilitários de conferência (sem servidor)

Testar os coletores contra um município real:

```powershell
.\.venv\Scripts\python teste_coletores.py 4202404 83108357000115
```

Ver o scorecard detalhado da última análise (ou de um id específico):

```powershell
.\.venv\Scripts\python ver_scorecard.py
```

Recalcular o score com o código atual sobre dados já coletados — útil para avaliar o efeito
de uma mudança de peso sem refazer a coleta:

```powershell
.\.venv\Scripts\python ver_scorecard.py --recalcular
```

Comparar municípios lado a lado, dimensão por dimensão. É a principal ferramenta de
validação: divergências de critério aparecem quando entes de perfis diferentes são postos
em coluna (sem argumentos, usa a última análise de cada município):

```powershell
.\.venv\Scripts\python comparar.py
```
