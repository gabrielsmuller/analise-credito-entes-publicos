# Metodologia do scorecard

Detalhamento das regras de pontuação e classificação de risco. O cálculo em si é
determinístico e está em [app/scoring.py](../app/scoring.py); o [README](../README.md) traz o
resumo e a tabela de pesos.

## Modalidade Simplificada dos municípios pequenos

Municípios com menos de 50 mil habitantes podem entregar os relatórios fiscais na forma
**Simplificada**: o RGF é semestral (`co_tipo_demonstrativo = "RGF Simplificado"`, periodicidade
`S`) e o RREO usa `"RREO Simplificado"`. A estrutura de contas é idêntica à completa — mesmos
`cod_conta` e anexos —, então os cálculos funcionam igual. O coletor tenta a modalidade completa
primeiro (a mais comum) e cai para a simplificada; a lógica fica em
[siconfi_base.py](../app/collectors/siconfi_base.py). Sem isso, entes pequenos apareciam com 45% de
cobertura e o bloco de pagamento ausente, como se não tivessem entregado nada — quando na verdade
tinham entregado na outra modalidade.

## Linguagem de risco

O sistema **mede e classifica** o risco de crédito; a IA **explica** os fatores e sugere
mitigadores; a **decisão comercial** de aceitar ou não a exposição é sempre do setor de licitações.
Por isso a IA nunca escreve "aprovar" ou "não aprovar" — fala em classificação de risco e cautelas.

A classificação é derivada do semáforo no código ([scoring.py](../app/scoring.py), campo
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

## Confiabilidade do score

Um score alto sobre poucos dados passa falsa segurança. Quando os demonstrativos fiscais não são
localizados (nem na modalidade simplificada), o bloco de comportamento de pagamento — o mais
pesado — fica ausente, e o score passa a vir só de indicadores periféricos. Nesse caso o score é
marcado como **não confiável**: o semáforo não sobe além de amarelo, o anel do score aparece
desbotado, um aviso destacado explica a limitação e a IA abre o resumo por ela. A redação diz que
os dados "não foram localizados nas consultas ao SICONFI", não que o município "não entregou" — a
distinção importa, porque só o extrato oficial de entregas autorizaria a segunda afirmação.

## Restrições e ressalvas

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

## Critérios que exigem trajetória, não só o último dado

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

## O que é exibido mas não pontua, e por quê

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
