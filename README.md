# analise-resultados-noworkflow
Proposta de trabalho da diciplina de eScience, uso do NoWorkflow para captura de proveniência em script cientifico.

Para usar nosso trabalho, precisa-se instalar o NoWorkFlow, copiando-o do github, não o instale pelo pip, senão encontrará erros.

1. Dentre Z execuções, qual função rodou por mais tempo? - Caio
2. Dada uma função X, quais funções a chamaram? - Lorhan 
3. Qual foi o valor de retorno da função Y? - Lorhan
4. Quais funções não foram chamadas na última execução? - Caio
6. Impactos entre diferentes valores de um certo parâmetro? - Caio
5. Qual foi a primeira variável que divergiu entre dois trials? - Guilherme 
7. Qual variável teve o valor mais impactado entre dois trials? - Guilherme
8. Quais valores uma certa variável teve ao longo da execução? - Guilherme
9. Quais versões e bilbiotecas foram usadas em cada trial?  - Lorhan

## Prolog (necessário para as consultas em Prolog)

No Windows, instale o interpretador e a ponte Python:

```powershell
winget install --id SWI-Prolog.SWI-Prolog -e   # SWI-Prolog (interpretador)
pip install pyswip                              # ponte Python -> SWI-Prolog
```

As consultas que usam só SQL funcionam sem isso; o Prolog é exigido apenas
pelas análises recursivas (ex.: "por que o resultado mudou?").

