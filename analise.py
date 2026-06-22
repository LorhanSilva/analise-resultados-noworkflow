"""
analise.py - Ferramenta de analise de proveniencia do noWorkflow
================================================================

Esta ferramenta le a proveniencia capturada pelo noWorkflow (o banco
.noworkflow/db.sqlite) e ajuda a responder perguntas do tipo:

    - Qual funcao rodou por mais tempo?
    - Quais funcoes foram definidas mas nunca chamadas?
    - Comparando duas execucoes (trials), qual variavel mudou mais?
    - Por que o resultado mudou? (grafo de dependencia que levou ao valor)

Ela usa DUAS vias de consulta, escolhendo a melhor para cada caso:

    SQL    -> perguntas de contagem/agregacao/comparacao (nao recursivas).
    Prolog -> perguntas recursivas, como o grafo de dependencia (slicing),
              usando uma regra recursiva sobre o grafo de dependencias.

A ferramenta e GENERICA: funciona para qualquer script que tenha sido
executado com `now run`, nao apenas para o experimento deste projeto.

Como usar:
    python analise.py        -> abre um menu interativo

Requisitos da via Prolog: SWI-Prolog instalado + pacote pyswip.
"""

import os
import sys
import sqlite3
import tempfile
import atexit

# No Windows, o terminal costuma usar a codificacao cp1252, que nao consegue
# imprimir caracteres como "->" ou acentos que aparecem nos dados capturados.
# Forcamos UTF-8 na saida para a ferramenta funcionar em qualquer maquina.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:
    pass


# ----------------------------------------------------------------------------
# CONFIGURACAO
# ----------------------------------------------------------------------------

# Pasta onde este arquivo esta (assim a ferramenta funciona de qualquer lugar)
PASTA = os.path.dirname(os.path.abspath(__file__))
BANCO = os.path.join(PASTA, ".noworkflow", "db.sqlite")

# Locais comuns do SWI-Prolog no Windows (para o pyswip achar a DLL)
PASTAS_SWIPL = [
    r"C:\Program Files\swipl\bin",
    r"C:\Program Files (x86)\swipl\bin",
]


# ----------------------------------------------------------------------------
# CAMADA SQL  -  acesso direto ao banco de proveniencia
# ----------------------------------------------------------------------------

def consultar_sql(query, parametros=()):
    """Roda uma consulta SQL no banco e devolve a lista de linhas."""
    conexao = sqlite3.connect(BANCO)
    conexao.row_factory = sqlite3.Row   # permite acessar colunas pelo nome
    try:
        return conexao.execute(query, parametros).fetchall()
    finally:
        conexao.close()


def listar_trials():
    """
    Devolve os trials em ordem de execucao. A cada um damos um numero
    sequencial (1, 2, 3, ...), igual ao que o comando `now list` mostra.
    """
    linhas = consultar_sql(
        "SELECT id, command, status FROM trial ORDER BY start"
    )
    trials = []
    numero = 1
    for linha in linhas:
        trials.append({
            "n": numero,
            "id": linha["id"],
            "command": linha["command"],
            "status": linha["status"],
        })
        numero = numero + 1
    return trials


def id_do_trial(numero):
    """Converte o numero sequencial (1, 2, ...) no id (UUID) do trial."""
    for trial in listar_trials():
        if trial["n"] == int(numero):
            return trial["id"]
    raise ValueError("Nao existe trial numero " + str(numero))


def ultima_execucao():
    """
    Numero do ultimo trial FINALIZADO (a 'ultima execucao').
    Ignora trials 'unfinished' (execucao interrompida), cuja proveniencia e
    incompleta e nao serve para dizer o que foi ou nao chamado.
    Devolve None se nao houver nenhuma execucao finalizada.
    """
    finalizados = [t["n"] for t in listar_trials() if t["status"] == "finished"]
    if finalizados:
        return finalizados[-1]
    return None


# ----------------------------------------------------------------------------
# CAMADA PROLOG  -  consulta recursiva sobre o grafo de dependencias
# ----------------------------------------------------------------------------
#
# Usamos o Prolog so para o que ele faz de melhor: recursao. A pergunta "de
# que tudo um valor dependeu?" e um fecho transitivo no grafo de dependencias
# da proveniencia - dificil em SQL, natural em Prolog.
#
# Por isso so precisamos de UM tipo de fato: a dependencia direta entre dois
# valores. Nos mesmos geramos esses fatos a partir do banco (rapido), em vez
# de exportar toda a proveniencia. A regra recursiva depende_de faz o resto.
#
# Toda a parte "chata" de falar com o Prolog fica encapsulada em
# consultar_prolog(); o resto do programa so chama essa funcao.

# Regra recursiva: depende_de(Trial, A, B) e verdadeiro quando o valor A
# dependeu, direta ou indiretamente, do valor B.
# O ":- table" (tabulacao) guarda resultados ja calculados; sem isso a recursao
# sobre centenas de milhares de dependencias seria lenta. Com ele, responde em
# fracoes de segundo.
REGRA_DEPENDE_DE = """
:- table depende_de/3.
depende_de(Trial, A, B) :-
    dependencia(Trial, A, B).
depende_de(Trial, A, B) :-
    dependencia(Trial, A, Meio),
    depende_de(Trial, Meio, B).
"""

# "Memoria" da via Prolog, para nao recarregar tudo a cada consulta.
_prolog = None              # o motor pyswip (criado uma unica vez)
_trials_carregados = []     # numeros de trials ja carregados no motor
_regra_carregada = False
_arquivos_temporarios = []  # .pl temporarios, removidos quando o programa fecha


def _carregar_no_prolog(texto):
    """Grava o texto num arquivo .pl temporario e o carrega no motor Prolog."""
    descritor, caminho = tempfile.mkstemp(suffix=".pl", text=True)
    arquivo = os.fdopen(descritor, "w", encoding="utf-8")
    arquivo.write(texto)
    arquivo.close()
    _arquivos_temporarios.append(caminho)
    # o pyswip precisa do caminho com barras normais
    _prolog.consult(caminho.replace("\\", "/"))


def _carregar_dependencias(numero):
    """
    Le do banco as dependencias diretas do trial e as transforma em fatos
    Prolog 'dependencia(Trial, A, B)' (A dependeu diretamente de B).
    """
    trial = id_do_trial(numero)
    linhas = consultar_sql(
        "SELECT dependent_id, dependency_id FROM dependency WHERE trial_id = ?",
        (trial,))
    fatos = [":- dynamic(dependencia/3)."]
    for linha in linhas:
        fatos.append("dependencia('%s', %d, %d)."
                     % (trial, linha["dependent_id"], linha["dependency_id"]))
    _carregar_no_prolog("\n".join(fatos))


def consultar_prolog(numero, pergunta):
    """
    Roda uma consulta Prolog sobre o trial 'numero'.

    Devolve uma lista de respostas; cada resposta e um dicionario que liga o
    nome de cada variavel da pergunta ao valor encontrado.

    Exemplo:
        consultar_prolog(3, "depende_de('UUID', 100, B)")
    """
    global _prolog, _regra_carregada

    # 1) Liga o motor Prolog na primeira vez que for usado.
    if _prolog is None:
        for pasta in PASTAS_SWIPL:
            if os.path.isdir(pasta):
                os.add_dll_directory(pasta)
        from pyswip import Prolog
        _prolog = Prolog()

    # 2) Carrega as dependencias do trial (so uma vez por trial).
    if numero not in _trials_carregados:
        _carregar_dependencias(numero)
        _trials_carregados.append(numero)
        # como entraram fatos novos, limpamos o cache da tabulacao
        if _regra_carregada:
            list(_prolog.query("abolish_all_tables"))

    # 3) Carrega a regra recursiva (so uma vez).
    if not _regra_carregada:
        _carregar_no_prolog(REGRA_DEPENDE_DE)
        _regra_carregada = True

    # 4) Executa a pergunta e devolve as respostas como lista.
    return list(_prolog.query(pergunta))


def _limpar_temporarios():
    """Remove os arquivos .pl temporarios ao encerrar o programa."""
    for caminho in _arquivos_temporarios:
        try:
            os.remove(caminho)
        except OSError:
            pass


atexit.register(_limpar_temporarios)


# ----------------------------------------------------------------------------
# CONSULTAS DE ANALISE  (cada uma responde uma pergunta do trabalho)
# ----------------------------------------------------------------------------

def duracoes_por_funcao(numero):
    """
    >>> Responde a PERGUNTA 4: "Qual funcao rodou por mais tempo?" (1 execucao)
        A funcao campea e a primeira da lista retornada (maior tempo total).

    [SQL] Tempo total gasto em cada funcao do trial.

    A duracao de uma chamada e (instante em que terminou) menos (instante em
    que comecou). No noWorkflow isso e: evaluation.checkpoint - activation.
    start_checkpoint. Somamos por funcao.
    """
    query = """
        SELECT cc.name AS funcao,
               COUNT(*) AS chamadas,
               ROUND(SUM(e.checkpoint - a.start_checkpoint), 4) AS tempo
        FROM activation a
        JOIN evaluation e      ON e.trial_id = a.trial_id AND e.id = a.id
        JOIN code_component cc ON cc.trial_id = a.trial_id
                              AND cc.id = e.code_component_id
        WHERE a.trial_id = ?
        GROUP BY cc.name
        ORDER BY tempo DESC
    """
    return consultar_sql(query, (id_do_trial(numero),))


def funcoes_nao_chamadas(numero):
    """
    >>> Responde as PERGUNTAS 5 e 2: "Quais funcoes nao foram chamadas?"
        Pergunta 5: passe o trial desejado.
        Pergunta 2: passe o ultimo trial finalizado (a "ultima execucao").

    [SQL] Funcoes que foram DEFINIDAS no codigo mas NUNCA chamadas no trial.

    Uma funcao foi chamada se existe alguma 'activation' apontando para o
    bloco de codigo dela (activation.code_block_id = id da definicao).
    """
    query = """
        SELECT cc.name AS funcao
        FROM code_component cc
        WHERE cc.trial_id = ?
          AND cc.type = 'function_def'
          AND NOT EXISTS (
              SELECT 1 FROM activation a
              WHERE a.trial_id = cc.trial_id
                AND a.code_block_id = cc.id
          )
        ORDER BY cc.name
    """
    return consultar_sql(query, (id_do_trial(numero),))


def _texto_para_numero(texto):
    """Tenta converter o texto de um valor para numero; senao, devolve None."""
    try:
        return float(texto)
    except (TypeError, ValueError):
        return None


def valores_escalares(numero):
    """
    [SQL] Coleta os valores numericos do trial, de forma generica.

    Cada 'evaluation' tem um repr (o valor que aquela parte do codigo
    produziu). Pegamos os trechos de codigo cujo valor foi um unico numero
    bem definido (ignorando os que variam, como contadores de laco). O
    resultado e um dicionario: nome do trecho -> valor numerico.
    """
    query = """
        SELECT cc.name AS nome, e.repr AS valor
        FROM evaluation e
        JOIN code_component cc ON cc.trial_id = e.trial_id
                              AND cc.id = e.code_component_id
        WHERE e.trial_id = ?
    """
    valores_vistos = {}   # nome -> conjunto de numeros distintos
    for linha in consultar_sql(query, (id_do_trial(numero),)):
        numero_valor = _texto_para_numero(linha["valor"])
        if numero_valor is None:
            continue
        nome = linha["nome"]
        if nome not in valores_vistos:
            valores_vistos[nome] = set()
        valores_vistos[nome].add(numero_valor)

    # mantemos so os trechos que tiveram UM unico valor (sao "escalares")
    escalares = {}
    for nome in valores_vistos:
        if len(valores_vistos[nome]) == 1:
            escalares[nome] = valores_vistos[nome].pop()
    return escalares


def comparar_trials(numero_a, numero_b):
    """
    [SQL] Compara os valores escalares de dois trials e devolve, ordenado da
    maior para a menor mudanca, a lista de (nome, valor_a, valor_b, variacao).

    'variacao' e a mudanca relativa (em %), util para ranquear qual variavel
    foi a "mais impactada" pela diferenca entre as execucoes.
    """
    valores_a = valores_escalares(numero_a)
    valores_b = valores_escalares(numero_b)

    mudancas = []
    for nome in valores_a:
        if nome not in valores_b:
            continue
        va = valores_a[nome]
        vb = valores_b[nome]
        if va == vb:
            continue
        base = abs(va)
        if base == 0:
            base = 1.0
        variacao = abs(vb - va) / base * 100.0
        mudancas.append((nome, va, vb, variacao))

    mudancas.sort(key=lambda item: item[3], reverse=True)
    return mudancas


def _mapa_evaluations(numero):
    """
    Le, de uma vez, todas as 'evaluations' de um trial e devolve tres mapas
    uteis: id->nome do trecho, nome->lista de ids, e id->linha no codigo.
    """
    trial = id_do_trial(numero)
    linhas = consultar_sql("""
        SELECT e.id AS id, cc.name AS nome, cc.first_char_line AS linha
        FROM evaluation e
        JOIN code_component cc ON cc.trial_id = e.trial_id
                              AND cc.id = e.code_component_id
        WHERE e.trial_id = ?
    """, (trial,))
    nome_por_id = {}
    ids_por_nome = {}
    linha_por_id = {}
    for linha in linhas:
        nome_por_id[linha["id"]] = linha["nome"]
        linha_por_id[linha["id"]] = linha["linha"]
        if linha["nome"] not in ids_por_nome:
            ids_por_nome[linha["nome"]] = []
        ids_por_nome[linha["nome"]].append(linha["id"])
    return nome_por_id, ids_por_nome, linha_por_id


def dependencias_de(numero, ids):
    """
    [PROLOG] Conjunto de ids de valores dos quais os 'ids' dependem (direta ou
    indiretamente). Usa a regra recursiva depende_de.
    """
    trial = id_do_trial(numero)
    alcancados = set()
    for um_id in ids:
        pergunta = "depende_de('%s', %d, B)" % (trial, um_id)
        for resposta in consultar_prolog(numero, pergunta):
            alcancados.add(resposta["B"])
    return alcancados


def explicar_mudanca(numero_a, numero_b):
    """
    [SQL + PROLOG] Separa as variaveis que mudaram em CAUSAS e RESULTADOS,
    usando a propria proveniencia.

    Para cada valor que mudou, perguntamos ao Prolog de quais OUTROS valores
    que tambem mudaram ele depende. Com isso classificamos:

        - CAUSA    : nao depende de nenhum outro valor que mudou (uma entrada
                     que voce alterou, ex.: um parametro).
        - RESULTADO: depende de algum valor que mudou E nenhum outro valor que
                     mudou depende dele (um valor "final", ex.: uma metrica).

    Os valores no meio do caminho (copias/intermediarios) sao omitidos para a
    explicacao ficar limpa.

    Devolve (causas, resultados):
        causa     = (nome, valor_a, valor_b, variacao)
        resultado = (nome, valor_a, valor_b, variacao, conjunto_de_causas)
    """
    mudancas = comparar_trials(numero_a, numero_b)
    nomes = [item[0] for item in mudancas]
    valores = {}
    for nome, va, vb, variacao in mudancas:
        valores[nome] = (va, vb, variacao)
    mudaram = set(nomes)
    nome_por_id, ids_por_nome, _ = _mapa_evaluations(numero_b)

    # de quais valores-que-mudaram cada valor depende
    depende = {}
    for nome in nomes:
        ids = ids_por_nome.get(nome, [])
        alcancados = dependencias_de(numero_b, ids)
        nomes_alcancados = set(nome_por_id[i] for i in alcancados if i in nome_por_id)
        depende[nome] = (nomes_alcancados & mudaram) - set([nome])

    # algum valor-que-mudou depende deste? (se sim, ele nao e "final")
    alguem_depende = set()
    for nome in depende:
        alguem_depende = alguem_depende | depende[nome]

    nomes_causa = set(nome for nome in nomes if not depende[nome])

    causas = []
    resultados = []
    for nome in nomes:
        va, vb, variacao = valores[nome]
        if nome in nomes_causa:
            causas.append((nome, va, vb, variacao))
        elif nome not in alguem_depende:
            raizes = depende[nome] & nomes_causa
            resultados.append((nome, va, vb, variacao, raizes))
        # senao: valor intermediario, omitido
    return causas, resultados


# ----------------------------------------------------------------------------
# APRESENTACAO  (impressao amigavel no terminal)
# ----------------------------------------------------------------------------

def _titulo(texto):
    print("")
    print("=" * 70)
    print(texto)
    print("=" * 70)


def mostrar_trials():
    _titulo("TRIALS DISPONIVEIS")
    for trial in listar_trials():
        print("  [%d] %-10s %s" % (trial["n"], trial["status"], trial["command"]))


def mostrar_duracoes(numero, limite=15):
    _titulo("TEMPO POR FUNCAO - trial %d  [SQL]" % numero)
    linhas = duracoes_por_funcao(numero)
    for linha in linhas[:limite]:
        nome = linha["funcao"].replace("\n", " ")
        print("  %9.4fs  (%dx)  %s" % (linha["tempo"], linha["chamadas"],
                                       nome[:50]))
    if len(linhas) > limite:
        print("  ... (%d outras)" % (len(linhas) - limite))


def mostrar_funcoes_nao_chamadas(numero):
    _titulo("FUNÇÕES DEFINIDAS MAS NÃO CHAMADAS - trial %d  [SQL]" % numero)
    linhas = funcoes_nao_chamadas(numero)
    if not linhas:
        print("  (todas as funções definidas foram chamadas)")
    for linha in linhas:
        print("  -", linha["funcao"])


def mostrar_comparacao(numero_a, numero_b):
    _titulo("VARIAVEIS QUE MUDARAM  -  trial %d  vs  trial %d  [SQL]"
            % (numero_a, numero_b))
    mudancas = comparar_trials(numero_a, numero_b)
    if not mudancas:
        print("  (nenhum valor escalar mudou entre os dois trials)")
        return
    print("  %-40s %12s %12s %10s" % ("variavel", "trial " + str(numero_a),
                                      "trial " + str(numero_b), "variacao"))
    print("  " + "-" * 76)
    for nome, va, vb, variacao in mudancas[:15]:
        print("  %-40s %12g %12g %9.1f%%" % (nome[:40], va, vb, variacao))


def mostrar_por_que_mudou(numero_a, numero_b):
    _titulo("POR QUE O RESULTADO MUDOU?  trial %d vs %d  [SQL + PROLOG]"
            % (numero_a, numero_b))
    causas, resultados = explicar_mudanca(numero_a, numero_b)

    if not causas and not resultados:
        print("  (nenhum valor escalar mudou; nada a explicar)")
        return

    print("  ENTRADAS QUE VOCÊ MUDOU (causas):")
    if not causas:
        print("    (nenhuma)")
    for nome, va, vb, variacao in causas[:10]:
        print("    %-40s %g -> %g" % (nome[:40], va, vb))

    print("")
    print("  RESULTADOS AFETADOS:")
    if not resultados:
        print("    (nenhum resultado dependente mudou)")
        return
    for nome, va, vb, variacao, causas_dele in resultados[:10]:
        print("    %-40s %g -> %g  (%.1f%%)" % (nome[:40], va, vb, variacao))
        print("        por causa de: " + ", ".join(sorted(causas_dele)[:4]))


def resumo_geral():
    """Roda todas as analises de uma vez (pode ficar longo)."""
    mostrar_trials()

    finalizados = [t["n"] for t in listar_trials() if t["status"] == "finished"]
    if not finalizados:
        print("\nNao ha trials finalizados para analisar.")
        return

    primeiro = finalizados[0]
    mostrar_duracoes(primeiro)
    mostrar_funcoes_nao_chamadas(primeiro)

    if len(finalizados) >= 2:
        a = finalizados[0]
        b = finalizados[1]
        mostrar_comparacao(a, b)
        mostrar_por_que_mudou(a, b)
    else:
        print("\n(Apenas um trial finalizado: nao da para comparar.)")


# ============================================================================
# PERGUNTAS DO ENUNCIADO  -  MAPA E FUNCOES A IMPLEMENTAR
# ============================================================================
#
# Mapa de qual funcao resolve qual pergunta. As perguntas JA RESOLVIDAS tem,
# mais acima no arquivo, um comentario "# Responde: Pergunta N" na funcao
# correspondente. As que FALTAM estao abaixo, como funcoes vazias (stubs)
# para implementar (procure o "TODO").
#
#   Pergunta 1 (funcao mais demorada entre TODAS as execucoes) -> pergunta_1_...        [A IMPLEMENTAR]
#   Pergunta 2 (funcoes nao chamadas na ULTIMA execucao)       -> funcoes_nao_chamadas()    [PRONTA]
#   Pergunta 3 (Qual foi a primeira variavel que divergiu entre dois trials) -> pergunta_3_primeira_divergencia()        [PRONTA]
#   Pergunta 4 (funcao que rodou por mais tempo em 1 execucao) -> duracoes_por_funcao()     [PRONTA]
#   Pergunta 5 (funcoes nao chamadas NESTA execucao)           -> funcoes_nao_chamadas()    [PRONTA]
#   Pergunta 6 (dada a funcao X, quais funcoes a chamaram)     -> pergunta_6_...        [A IMPLEMENTAR]
#   Pergunta 7 (valor de retorno da funcao Y)                  -> pergunta_7_...        [A IMPLEMENTAR]
#   Pergunta 8 (trial e reproduzivel em relacao ao anterior)   -> pergunta_8_...        [A IMPLEMENTAR]
#   Pergunta 9 (Qual variável teve o valor mais impactado entre dois trials) -> pergunta_9_maior_divergencia()        [PRONTA]
#   Pergunta 10 (Quais valores uma certa variável teve ao longo da execução) -> pergunta_10_historico()        [PRONTA]
# ----------------------------------------------------------------------------


def pergunta_1_funcao_mais_demorada_geral(numeros=None):
    """
    [Dono: Caio] PERGUNTA 1: "Dentre Z execucoes, qual funcao rodou por mais tempo?"

    numeros: lista de numeros de trial a considerar.
             None ou lista vazia = TODAS as execucoes finalizadas.
             Ex.: pergunta_1_funcao_mais_demorada_geral([2, 3, 4]).

    Usa o TEMPO PROPRIO de cada funcao: o tempo gasto NELA MESMA, descontando
    o tempo das funcoes que ela chamou. Assim o ranking aponta o gargalo real
    e nao wrappers como o proprio script ou o main(), que "duram" muito so
    porque chamam todo o resto.

    Tempo total de uma chamada  = fim - inicio
        (fim = evaluation.checkpoint; inicio = activation.start_checkpoint)
    Tempo proprio de uma chamada = total - soma do total das filhas diretas
        (a filha de uma ativacao e quem tem activation_id apontando para ela)

    O resultado e por (execucao, funcao). [SQL + calculo em Python]
    """
    # 1) decide quais trials entram na conta
    if numeros:
        trials = [t for t in listar_trials() if t["n"] in numeros]
    else:
        trials = [t for t in listar_trials() if t["status"] == "finished"]
    if not trials:
        print("Nenhuma execução para analisar.")
        return

    # 2) para cada execucao, calcula o tempo proprio de cada funcao
    proprio_por_funcao = {}   # (numero_trial, nome) -> tempo proprio somado
    chamadas = {}             # (numero_trial, nome) -> quantas vezes rodou
    for t in trials:
        linhas = consultar_sql("""
            SELECT a.id AS id, a.start_checkpoint AS inicio,
                   e.checkpoint AS fim, e.activation_id AS pai,
                   cc.name AS nome, cc.type AS tipo
            FROM activation a
            JOIN evaluation e      ON e.trial_id = a.trial_id AND e.id = a.id
            JOIN code_component cc ON cc.trial_id = a.trial_id
                                  AND cc.id = e.code_component_id
            WHERE a.trial_id = ?
        """, (t["id"],))

        total = {}        # id -> tempo total (inclusivo)
        nome = {}         # id -> nome da funcao
        tipo = {}         # id -> tipo do code_component ('script', 'call', ...)
        soma_filhas = {}  # id -> soma do total das filhas diretas
        for linha in linhas:
            total[linha["id"]] = linha["fim"] - linha["inicio"]
            nome[linha["id"]] = linha["nome"]
            tipo[linha["id"]] = linha["tipo"]
        for linha in linhas:
            pai = linha["pai"]
            if pai in total:
                soma_filhas[pai] = soma_filhas.get(pai, 0.0) + total[linha["id"]]

        for ident in total:
            # ignora a ativacao raiz (o proprio script, tipo 'script'): o tempo
            # proprio dela e so import/startup, nao uma funcao do usuario
            if tipo[ident] == "script":
                continue
            proprio = total[ident] - soma_filhas.get(ident, 0.0)
            chave = (t["n"], nome[ident])
            proprio_por_funcao[chave] = proprio_por_funcao.get(chave, 0.0) + proprio
            chamadas[chave] = chamadas.get(chave, 0) + 1

    # 3) ordena por tempo proprio e mostra (campea + ranking)
    ranking = sorted(proprio_por_funcao.items(),
                     key=lambda item: item[1], reverse=True)
    if not ranking:
        print("Sem funções para mostrar nessas execuções.")
        return
    print("Execuções consideradas: " + ", ".join(str(t["n"]) for t in trials))
    print("Tempo próprio = tempo gasto na função, descontando as funções que ela chamou.")
    (trial_n, nome_f), tempo = ranking[0]
    print("Função que rodou por mais tempo: %s  (trial %d, %.4fs próprios)"
          % (" ".join(nome_f.split())[:55], trial_n, tempo))
    print("Ranking por tempo próprio — função @ execução (Nx = nº de chamadas):")
    for (trial_n, nome_f), tempo in ranking[:10]:
        curto = " ".join(nome_f.split())[:45]
        print("  %8.4fs  trial %d  %4dx  %s"
              % (tempo, trial_n, chamadas[(trial_n, nome_f)], curto))


def pergunta_6_quem_chamou(numero, nome_funcao):
    """
    >>> Responde a PERGUNTA 6: "Dada uma funcao X, quais funcoes a chamaram?"

     [SQL] Para cada chamada de X, sobe um nivel no grafo de ativacoes:
      1. Acha o id da definicao de X em code_component (type='function_def').
      2. Cada activation com code_block_id = esse id e uma chamada de X.
      3. A evaluation de mesmo id tem activation_id -> a ativacao "mae" (chamadora).
      4. Busca o nome da mae em activation; se nula, o chamador foi o nivel de
         modulo (codigo de topo).
    """
    query = """
        SELECT DISTINCT
            COALESCE(mae.name, '<modulo>') AS chamadora,
            COUNT(*) AS vezes
        FROM activation filha
        JOIN code_component cc
             ON cc.trial_id = filha.trial_id
            AND cc.id       = filha.code_block_id
            AND cc.type     = 'function_def'
            AND cc.name     = ?
        JOIN evaluation e
             ON e.trial_id = filha.trial_id
            AND e.id       = filha.id
        LEFT JOIN activation mae
             ON mae.trial_id = filha.trial_id
            AND mae.id       = e.activation_id
        WHERE filha.trial_id = ?
        GROUP BY chamadora
        ORDER BY vezes DESC
    """
    return consultar_sql(query, (nome_funcao, id_do_trial(numero)))


def mostrar_quem_chamou(numero, nome_funcao):
    _titulo("QUEM CHAMOU '%s' - trial %d  [SQL]" % (nome_funcao, numero))
    linhas = pergunta_6_quem_chamou(numero, nome_funcao)
    if not linhas:
        print("  (nenhuma chamada a '%s' encontrada neste trial)" % nome_funcao)
        return
    for linha in linhas:
        print("  %-40s  %dx" % (linha["chamadora"][:40], linha["vezes"]))



def pergunta_7_valor_de_retorno(numero, nome_funcao):
    """
    >>> Responde a PERGUNTA 7: "Qual foi o valor de retorno da funcao Y?"

    [SQL] Cada activation tem uma evaluation de MESMO id; o campo repr dessa
    evaluation e o valor que a chamada produziu (o retorno da funcao).
    Juntamos activation + evaluation + code_component, filtramos pelo nome
    em code_component e lemos evaluation.repr, ordenado pelo instante em que
    a chamada terminou (evaluation.checkpoint).
    """
    query = """
        SELECT
            a.id          AS chamada_id,
            e.repr        AS retorno,
            ROUND(e.checkpoint - a.start_checkpoint, 4) AS duracao
        FROM activation a
        JOIN evaluation e
             ON e.trial_id = a.trial_id
            AND e.id       = a.id
        JOIN code_component cc
             ON cc.trial_id = a.trial_id
            AND cc.id       = e.code_component_id
            AND cc.name     = ?
            AND cc.type     = 'function_def'
        WHERE a.trial_id = ?
        ORDER BY e.checkpoint
    """
    return consultar_sql(query, (nome_funcao, id_do_trial(numero)))


def mostrar_valor_de_retorno(numero, nome_funcao):
    _titulo("VALOR DE RETORNO DE '%s' - trial %d  [SQL]" % (nome_funcao, numero))
    linhas = pergunta_7_valor_de_retorno(numero, nome_funcao)
    if not linhas:
        print("  (nenhuma chamada a '%s' encontrada neste trial)" % nome_funcao)
        return
    for i, linha in enumerate(linhas, 1):
        retorno = (linha["retorno"] or "None")[:60]
        print("  Chamada %-3d  retorno: %-60s  (%ss)" % (
            i, retorno, linha["duracao"]))


def pergunta_8_reproduzivel(numero_a, numero_b):
    """
    [A IMPLEMENTAR] "O trial B e reproduzivel em relacao ao A (anterior)?"

    Ideia: B reproduz A se rodou o MESMO codigo, com as MESMAS entradas, e
    chegou aos MESMOS resultados.

    Como fazer (sugestao):
      - Compare o code hash dos dois trials (tabela trial, coluna code_hash):
        se diferente, o codigo mudou.
      - Compare as entradas (pode usar a coluna command do trial).
      - Use comparar_trials(a, b): se NENHUM resultado escalar divergiu, as
        saidas batem.
      - Conclua: reproduzivel = mesmo codigo + mesmas entradas + saidas iguais.
    """
    # TODO: implementar
    print("pergunta_8 ainda nao implementada")


def pergunta_3_primeira_divergencia(numero_a, numero_b):
    """
    [PRONTA] "Qual foi a PRIMEIRA variavel que divergiu entre dois trials?"

    Diferenca para comparar_trials(): la pegamos a de MAIOR mudanca; aqui
    queremos a PRIMEIRA no tempo (a que divergiu mais cedo na execucao).
    """
    trial_a = id_do_trial(numero_a)
    trial_b = id_do_trial(numero_b)
    
    # Busca todas as evaluations do trial B em ordem cronológica
    # e compara com as do trial A pelo nome do code_component
    query = """
        SELECT e.id, cc.name AS nome, e.repr AS valor, e.checkpoint, cc.first_char_line AS linha
        FROM evaluation e
        JOIN code_component cc ON cc.trial_id = e.trial_id AND cc.id = e.code_component_id
        WHERE e.trial_id = ?
        ORDER BY e.checkpoint ASC
    """
    
    evals_b = consultar_sql(query, (trial_b,))
    
    print("  Procurando primeira divergencia entre trial %d e %d..." % (numero_a, numero_b))
    print("  Verificando %d evaluations..." % len(evals_b))
    print("")
    
    # Para cada evaluation no trial B, busca o valor correspondente no trial A
    contador = 0
    for eval_b in evals_b:
        contador += 1
        nome = eval_b["nome"]
        valor_b = eval_b["valor"]
        checkpoint = eval_b["checkpoint"]
        linha = eval_b["linha"]
        
        # Busca o mesmo componente no trial A
        query_a = """
            SELECT e.repr AS valor, e.checkpoint
            FROM evaluation e
            JOIN code_component cc ON cc.trial_id = e.trial_id AND cc.id = e.code_component_id
            WHERE e.trial_id = ? AND cc.name = ?
            ORDER BY e.checkpoint ASC
            LIMIT 1
        """
        result_a = consultar_sql(query_a, (trial_a, nome))
        
        if result_a:
            valor_a = result_a[0]["valor"]
            checkpoint_a = result_a[0]["checkpoint"]
            
            if valor_a != valor_b:
                # Encontrou a primeira divergência
                print("=" * 70)
                print("  PRIMEIRA DIVERGENCIA ENCONTRADA!")
                print("=" * 70)
                print("")
                print("  VARIAVEL: %s" % nome)
                print("  LINHA DO CODIGO: %d" % linha if linha > 0 else "  LINHA: (interna)")
                print("")
                print("  VALORES:")
                print("    Trial %d: %s" % (numero_a, valor_a))
                print("    Trial %d: %s" % (numero_b, valor_b))
                print("")
                print("  MOMENTO DA EXECUCAO:")
                print("    Trial %d checkpoint: %f" % (numero_a, checkpoint_a))
                print("    Trial %d checkpoint: %f" % (numero_b, checkpoint))
                print("    Ordem da divergencia: %d de %d evaluations" % (contador, len(evals_b)))
                print("")
                
                # Calcula a diferença se forem números
                try:
                    num_a = float(valor_a)
                    num_b = float(valor_b)
                    diferenca = abs(num_b - num_a)
                    percentual = (diferenca / abs(num_a) * 100) if num_a != 0 else float('inf')
                    print("  DIFERENCA NUMERICA:")
                    print("    Diferenca absoluta: %g" % diferenca)
                    if num_a != 0:
                        print("    Variacao percentual: %.1f%%" % percentual)
                    print("")
                except (TypeError, ValueError):
                    pass
                
                return
    
    print("  Nenhuma divergencia encontrada entre os trials.")
    print("  Todos os valores comparados foram identicos.")
    
def pergunta_9_maior_divergencia(numero_a, numero_b):
    """
    [PRONTA] "Qual foi a variavel que divergiu mais entre dois trials?"
    
    Usando comparar_trials já conseguimos coletar todas as mudanças, logo, basta extrair os valores mais altos.

    """
    _titulo("VARIAVEL MAIS IMPACTADA  -  trial %d  vs  trial %d  [SQL]"
            % (numero_a, numero_b))
    mudancas = comparar_trials(numero_a, numero_b)
    
    if not mudancas:
        print("  (nenhum valor escalar mudou entre os dois trials)")
        return
    
    # A primeira já é a de maior variação (ordenada por variacao DESC)
    nome, va, vb, variacao = mudancas[0]
    
    print("  VARIAVEL MAIS IMPACTADA:")
    print("    Nome: %s" % nome)
    print("    Trial %d: %g" % (numero_a, va))
    print("    Trial %d: %g" % (numero_b, vb))
    print("    Variacao: %.1f%%" % variacao)
    print("")
    
    # Mostra as outras variações para contexto
    print("  TOP 5 VARIACOES:")
    print("  %-40s %12s %12s %10s" % ("variavel", "trial " + str(numero_a),
                                      "trial " + str(numero_b), "variacao"))
    print("  " + "-" * 76)
    for nome, va, vb, variacao in mudancas[:5]:
        print("  %-40s %12g %12g %9.1f%%" % (nome[:40], va, vb, variacao))
    
    
def pergunta_10_historico(numero): # Revisar
    """
    [PRONTA] "Qual foram os valores que uma a variavel w conteu durante uma execução?"

    """
    trial = id_do_trial(numero)
    
    # Lista todas as variáveis disponíveis no trial
    query_variaveis = """
        SELECT DISTINCT cc.name AS nome
        FROM evaluation e
        JOIN code_component cc ON cc.trial_id = e.trial_id AND cc.id = e.code_component_id
        WHERE e.trial_id = ?
        ORDER BY cc.name
    """
    variaveis = consultar_sql(query_variaveis, (trial,))
    
    if not variaveis:
        print("  Nenhuma variavel encontrada no trial %d" % numero)
        return
    
    print("  VARIAVEIS DISPONIVEIS NO TRIAL %d:" % numero)
    for i, var in enumerate(variaveis[:20]):
        print("    %d. %s" % (i+1, var["nome"]))
    if len(variaveis) > 20:
        print("    ... e mais %d variaveis" % (len(variaveis) - 20))
    
    nome_variavel = input("Digite o nome da variavel W: ").strip()
    
    # Busca todos os valores da variável em ordem cronológica
    query = """
        SELECT e.repr AS valor, e.checkpoint, cc.first_char_line AS linha
        FROM evaluation e
        JOIN code_component cc ON cc.trial_id = e.trial_id AND cc.id = e.code_component_id
        WHERE e.trial_id = ? AND cc.name = ?
        ORDER BY e.checkpoint ASC
    """
    resultados = consultar_sql(query, (trial, nome_variavel))
    
    if not resultados:
        print("  Variavel '%s' nao encontrada no trial %d" % (nome_variavel, numero))
        return
    
    _titulo("HISTORICO DA VARIAVEL '%s' - trial %d" % (nome_variavel, numero))
    
    print("  %-30s %-20s %12s" % ("Valor", "Linha", "Checkpoint"))
    print("  " + "-" * 65)
    
    for res in resultados:
        valor = res["valor"][:30] if res["valor"] else "None"
        linha = res["linha"] if res["linha"] > 0 else "-"
        print("  %-30s %-20s %12.4f" % (valor, linha, res["checkpoint"]))
    
    print("")
    print("  TOTAL DE REGISTROS: %d" % len(resultados))
    
    # Mostra estatísticas se for numérico
    valores_numericos = []
    for res in resultados:
        try:
            val = float(res["valor"])
            valores_numericos.append(val)
        except (TypeError, ValueError):
            pass
    
    if valores_numericos:
        print("  ESTATISTICAS (valores numericos):")
        print("    Minimo: %g" % min(valores_numericos))
        print("    Maximo: %g" % max(valores_numericos))
        print("    Media: %g" % (sum(valores_numericos) / len(valores_numericos)))
        print("    Total de valores numericos: %d" % len(valores_numericos))


# ----------------------------------------------------------------------------
# MENU INTERATIVO
# ----------------------------------------------------------------------------

def _perguntar_numero(texto):
    """Le um numero de trial digitado pelo usuario."""
    return int(input(texto).strip())


def menu_perguntas():
    """Submenu com as 10 perguntas do enunciado (chama as funcoes pergunta_*)."""
    while True:
        print("")
        print("------------------- PERGUNTAS DO ENUNCIADO -------------------")
        print("  1 - [P1] Funcao mais demorada entre TODAS as execucoes  [A IMPLEMENTAR]")
        print("  2 - [P2] Funcoes nao chamadas na ULTIMA execucao        [PRONTA]")
        print("  3 - [P3] Primeira variavel que divergiu (2 trials)      [PRONTA]")#Minha
        print("  4 - [P4] Funcao que rodou por mais tempo (1 execucao)   [PRONTA]")
        print("  5 - [P5] Funcoes nao chamadas nesta execucao            [PRONTA]")
        print("  6 - [P6] Dada a funcao X, quais funcoes a chamaram      [PRONTA]")
        print("  7 - [P7] Valor de retorno da funcao Y                   [PRONTA]")
        print("  8 - [P8] Trial reproduzivel em relacao ao anterior      [A IMPLEMENTAR]")
        print("  9 - [P9] Variável mais impactada (2 trials)      [PRONTA]")#Minha
        print("  10 - [P10] Valores da variável W (1 execucao)     [PRONTA]")#Minha
        print("  9 - [P9] Versoes e bibliotecas usadas em cada trial     [PRONTA]")
        print("  v - Voltar")
        opcao = input("Escolha: ").strip().lower()

        if opcao == "v":
            break
        elif opcao == "1":
            entrada = input("Trials (vazio/0 = todas, ou ex.: 2,3,4): ").strip()
            if entrada == "" or entrada == "0":
                numeros = None
            else:
                numeros = [int(x) for x in entrada.replace(",", " ").split()]
            pergunta_1_funcao_mais_demorada_geral(numeros)
        elif opcao == "2":
            mostrar_trials()
            entrada = input("Trial (vazio = última execução): ").strip()
            if entrada == "":
                numero = ultima_execucao()
                if numero is None:
                    print("Não há execução finalizada.")
                    continue
                print("Última execução: trial %d" % numero)
            else:
                numero = int(entrada)
            mostrar_funcoes_nao_chamadas(numero)
        elif opcao == "3":
            mostrar_trials()
            a = _perguntar_numero("Primeiro trial: ")
            b = _perguntar_numero("Segundo trial: ")
            pergunta_3_primeira_divergencia(a, b)
        elif opcao == "4":
            mostrar_trials()
            mostrar_duracoes(_perguntar_numero("Numero do trial: "))
        elif opcao == "5":
            mostrar_trials()
            mostrar_funcoes_nao_chamadas(_perguntar_numero("Numero do trial: "))
        elif opcao == "6":
            mostrar_trials()
            numero = _perguntar_numero("Numero do trial: ")
            nome = input("Nome da funcao X: ").strip()
            mostrar_quem_chamou(numero, nome)
        elif opcao == "7":
            mostrar_trials()
            numero = _perguntar_numero("Numero do trial: ")
            nome = input("Nome da funcao Y: ").strip()
            mostrar_valor_de_retorno(numero, nome)
        elif opcao == "8":
            mostrar_trials()
            a = _perguntar_numero("Trial anterior: ")
            b = _perguntar_numero("Trial atual: ")
            pergunta_8_reproduzivel(a, b)
        elif opcao == "9":
            mostrar_trials()
            a = _perguntar_numero("Primeiro trial: ")
            b = _perguntar_numero("Segundo trial: ")
            pergunta_9_maior_divergencia(a, b)
        elif opcao == "10":
            mostrar_trials()
            pergunta_10_historico(_perguntar_numero("Numero do trial: "))
        elif opcao == "9":
            mostrar_versoes_e_bibliotecas()
        else:
            print("Opcao invalida.")


def menu():
    while True:
        print("")
        print("==================== ANALISE DE PROVENIENCIA ====================")
        print("  0 - Resumo geral (roda tudo)")
        print("  1 - Listar trials")
        print("  2 - Tempo por funcao (1 trial)            [SQL]")
        print("  3 - Funcoes definidas mas nao chamadas    [SQL]")
        print("  4 - Comparar variaveis de 2 trials        [SQL]")
        print("  5 - Por que o resultado mudou? (2 trials) [SQL + Prolog]")
        print("  p - Perguntas do enunciado (P1..P8)")
        print("  q - Sair")
        opcao = input("Escolha: ").strip().lower()

        if opcao == "q":
            break
        elif opcao == "0":
            resumo_geral()
        elif opcao == "1":
            mostrar_trials()
        elif opcao == "2":
            mostrar_trials()
            mostrar_duracoes(_perguntar_numero("Numero do trial: "))
        elif opcao == "3":
            mostrar_trials()
            mostrar_funcoes_nao_chamadas(_perguntar_numero("Numero do trial: "))
        elif opcao == "4":
            mostrar_trials()
            a = _perguntar_numero("Primeiro trial: ")
            b = _perguntar_numero("Segundo trial: ")
            mostrar_comparacao(a, b)
        elif opcao == "5":
            mostrar_trials()
            a = _perguntar_numero("Primeiro trial: ")
            b = _perguntar_numero("Segundo trial: ")
            mostrar_por_que_mudou(a, b)
        elif opcao == "p":
            menu_perguntas()
        else:
            print("Opcao invalida.")


if __name__ == "__main__":
    menu()
