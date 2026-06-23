# analise-resultados-noworkflow
Proposta de trabalho da diciplina de eScience, uso do NoWorkflow para captura de proveniência em script cientifico.

Nós usamos consultas SQL e Prolog para facilitar o entendimento dos usuários da proveniênica gerada pelo NoWorkflow

# Requerimentos
Para usar nosso trabalho, precisa-se instalar o NoWorkFlow, copiando-o do github, não o instale pelo pip, senão encontrará erros.

## Prolog (necessário para as consultas em Prolog)

No Windows, instale o interpretador e a ponte Python:

```powershell
winget install --id SWI-Prolog.SWI-Prolog -e   # SWI-Prolog (interpretador)
pip install pyswip                              # ponte Python -> SWI-Prolog
```

As consultas que usam só SQL funcionam sem isso; o Prolog é exigido apenas
pelas análises recursivas (ex.: "por que o resultado mudou?").

# Declaração de uso de IA

We have used DeepSeek and Claude to help us code. Despite that, we have revised all suggestions and made appropriate corrections. We thus take full responsibility for the contents of this work/report.


As perguntas seguem a numeração do menu da ferramenta (`analise.py`),
agrupadas por escopo:

# Panorama geral da ferramenta (perguntas que queremos responder)
Uma execução:

1. Quais funções não foram chamadas? (última execução ou trial escolhido)
2. Dada a função X, quais funções a chamaram?
3. Quais valores uma variável teve ao longo da execução?

Várias execuções:

4. Dentre Z execuções, qual função rodou por mais tempo?
5. Quais versões e bibliotecas foram usadas em cada trial?

Comparar DOIS trials:

6. Qual foi a primeira variável que divergiu entre dois trials?
7. Qual variável teve o valor mais impactado entre dois trials?
8. Impactos entre diferentes valores de um certo parâmetro? (por que o resultado mudou)
