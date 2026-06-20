# analise-resultados-noworkflow
Proposta de trabalho da diciplina de eScience, uso do NoWorkflow para captura de proveniência em script cientifico.

Para usar nosso trabalho, precisa-se instalar o NoWorkFlow, copiando-o do github, não o instale pelo pip, senão encontrará erros.

Dentre todas as execuções, qual função rodou por mais tempo?
Quais funções não foram chamadas na última execução?
“Qual foi a primeira variável que divergiu entre dois trials?”
"Qual função rodou por mais tempo?"
"Quais funções não foram chamadas nesta execução?"
"Dada uma função X, quais funções a chamaram?"
"Qual foi o valor de retorno da função Y?"
"Este trial é reproduzível em relação ao anterior?"

## Prolog (necessário para as consultas em Prolog)

No Windows, instale o interpretador e a ponte Python:

```powershell
winget install --id SWI-Prolog.SWI-Prolog -e   # SWI-Prolog (interpretador)
pip install pyswip                              # ponte Python -> SWI-Prolog
```

As consultas que usam só SQL funcionam sem isso; o Prolog é exigido apenas
pelas análises recursivas (ex.: "por que o resultado mudou?").

