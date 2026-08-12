# Análise de Crédito - Entes Públicos

Uma alternativa open source para análise de risco de crédito de municípios brasileiros.

O projeto consolida dados públicos fiscais, financeiros e institucionais para avaliar a situação de um município sob a perspectiva de quem fornece para o setor público.

A análise combina indicadores fiscais tradicionais com dados de **liquidez e comportamento de pagamento**, gerando um painel, classificação de risco e dossiê detalhado.

Inspirado em plataformas proprietárias como o MuniScore, mas com foco em uma implementação aberta, auditável e adaptável.

## Interface

![Análise de Crédito - Entes Públicos](docs/assets/app.png)

## Como funciona

Informe um município e o sistema:

1. identifica automaticamente o CNPJ oficial;
2. consulta as fontes públicas disponíveis;
3. calcula os indicadores e o score;
4. aplica restrições e critérios de confiabilidade;
5. gera o painel e o dossiê da análise;
6. salva o resultado no histórico.

Também é possível conversar com a IA sobre cada análise por meio de um chat contextual.

## Dados analisados

O projeto utiliza informações de:

* **SICONFI** - caixa, restos a pagar, RGF, RREO, RCL, despesas e dívida;
* **Tesouro Nacional** - CAPAG;
* **Portal da Transparência** - convênios, transferências e sanções;
* **PNCP** - contratos públicos;
* **IBGE** - população e PIB;
* **BrasilAPI** - dados cadastrais do CNPJ.

CAUC e Serasa podem ser informados manualmente.

## Metodologia

O score é calculado de forma determinística em `app/scoring.py`.

A análise considera:

| Indicador                   | Peso |
| --------------------------- | ---: |
| Liquidez para fornecedores  |   20 |
| Pagamento de restos a pagar |   12 |
| Execução de pagamento       |    8 |
| Tendência da liquidez       |    5 |
| CAPAG                       |   30 |
| Despesa com pessoal         |   10 |
| Dívida Consolidada Líquida  |   10 |
| Convênios federais          |    8 |
| Sanções                     |    8 |
| Porte populacional          |    4 |
| PIB per capita              |    3 |

O resultado é normalizado para uma escala de 0 a 100 considerando os indicadores disponíveis.

Além da nota, algumas condições podem limitar o semáforo, como:

* CAPAG C ou D;
* disponibilidade líquida negativa;
* sanções ativas;
* inadimplências relevantes em convênios;
* baixa cobertura de dados.

### Classificação de risco

| Semáforo      | Score de referência | Classificação            |
| ------------- | ------------------: | ------------------------ |
| Verde         |                ≥ 70 | Risco favorável          |
| Amarelo       |               57–69 | Risco moderado           |
| Amarelo       |               45–56 | Risco moderado a elevado |
| Vermelho      |                < 45 | Risco elevado            |
| Não confiável |                   — | Risco não avaliável      |

Restrições podem limitar o semáforo independentemente da pontuação.

## Comportamento de pagamento

Um dos principais focos do projeto é observar não apenas a situação fiscal do município, mas também sinais relacionados à capacidade e ao histórico de pagamento.

São analisados, entre outros:

* disponibilidade de caixa;
* obrigações financeiras;
* restos a pagar processados e não processados;
* percentuais pagos e cancelados;
* despesas liquidadas versus pagas;
* evolução da liquidez nos últimos exercícios.

Isso complementa indicadores como CAPAG, endividamento e despesa com pessoal.

## Inteligência artificial

A IA é utilizada para gerar o dossiê e responder perguntas sobre os dados coletados.

São suportados OpenAI e Anthropic.

### OpenAI

```env
LLM_PROVIDER=openai
OPENAI_API_KEY=
OPENAI_MODEL=
```

### Anthropic

```env
LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=
ANTHROPIC_MODEL=
```

## Instalação

Requer Python 3.11 ou superior.

Os exemplos abaixo utilizam Windows e PowerShell.

```powershell
git clone <URL_DO_REPOSITORIO>
cd analise-credito-entes-publicos

python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

copy .env.example .env
```

Configure o `.env`:

```env
OPENAI_API_KEY=
TRANSPARENCIA_API_KEY=

APP_USUARIOS=admin
APP_SENHA=
```

A chave do Portal da Transparência é necessária para consultar convênios, transferências e sanções.

## Executar

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Acesse:

```text
http://127.0.0.1:8000
```

Pesquise o município e gere a análise.

## Histórico

As análises e conversas são armazenadas localmente em SQLite:

```text
data/analises.db
```

## Estrutura principal

```text
app/
├── collectors/      # Coleta das fontes públicas
├── scoring.py       # Cálculo e classificação do risco
├── dossier.py       # Geração do dossiê
├── main.py          # Aplicação FastAPI
└── templates/       # Interface web
```

## Licença

MIT
