"""
experiment.py — Pipeline de classificação Titanic
==================================================
Uso com noWorkflow:
    now run experiment.py
    now run experiment.py --n_estimators 200 --max_depth 10 --age_strategy mean
    now run experiment.py --n_estimators 50  --max_depth 3  --age_strategy median

Uso direto (sem captura):
    python experiment.py --n_estimators 100 --max_depth 5

NOTA sobre SHAP:
    O noWorkflow captura a chamada de superfície ao SHAP (shap.TreeExplainer,
    explainer.shap_values) mas NÃO as chamadas internas, pois o SHAP executa
    internamente em C++/Cython. Ainda assim, o resultado final é capturado
    como valor de retorno da função compute_shap_importance().
"""

import argparse
import os
import warnings

import numpy as np
import pandas as pd
import shap
import matplotlib
matplotlib.use("Agg")           # sem display — compatível com now run
import matplotlib.pyplot as plt

from sklearn.ensemble import RandomForestClassifier
from sklearn.model_selection import train_test_split, cross_val_score
from sklearn.metrics import (
    accuracy_score, f1_score, roc_auc_score,
    classification_report, confusion_matrix,
)
from sklearn.preprocessing import LabelEncoder

warnings.filterwarnings("ignore")


# ──────────────────────────────────────────────────────────────────────────────
# 0. PARÂMETROS DO TRIAL
#    Cada argumento é um eixo de variação entre trials.
#    Mudar qualquer um deles cria um trial diferente para o painel comparar.
# ──────────────────────────────────────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(description="Titanic — pipeline noWorkflow")
    parser.add_argument("--data_path",     default="titanic.csv",  type=str)
    parser.add_argument("--test_size",     default=0.2,            type=float)
    parser.add_argument("--random_state",  default=42,             type=int)
    # --- imputação ---
    parser.add_argument("--age_strategy",  default="median",
                        choices=["median", "mean", "constant"],
                        help="Estratégia de imputação para Age")
    parser.add_argument("--age_constant",  default=29.0,           type=float,
                        help="Valor fixo quando age_strategy=constant")
    # --- modelo ---
    parser.add_argument("--n_estimators",  default=100,            type=int)
    parser.add_argument("--max_depth",     default=5,              type=int)
    parser.add_argument("--min_samples_split", default=2,          type=int)
    # --- saída ---
    parser.add_argument("--output_dir",    default="output",       type=str)
    return parser.parse_args()


# ──────────────────────────────────────────────────────────────────────────────
# 1. CARGA
# ──────────────────────────────────────────────────────────────────────────────

def load_data(path):
    """
    Carrega o CSV e faz validações básicas de integridade.
    Retorna o DataFrame bruto — sem nenhuma transformação.
    """
    if not os.path.exists(path):
        raise FileNotFoundError(f"Dataset não encontrado: {path}")

    df = pd.read_csv(path)

    expected_cols = {"Survived", "Pclass", "Name", "Sex", "Age",
                     "SibSp", "Parch", "Fare", "Embarked"}
    missing = expected_cols - set(df.columns)
    if missing:
        raise ValueError(f"Colunas ausentes no CSV: {missing}")

    n_rows, n_cols = df.shape
    n_missing_total = df.isnull().sum().sum()
    survival_rate = df["Survived"].mean()

    print(f"[load]  {n_rows} passageiros | {n_cols} colunas | "
          f"{n_missing_total} valores ausentes | "
          f"taxa de sobrevivência: {survival_rate:.1%}")

    return df


# ──────────────────────────────────────────────────────────────────────────────
# 2. EXTRAÇÃO DE FEATURES DO NOME
#    Extraída como função separada para aparecer como ativação própria no grafo.
# ──────────────────────────────────────────────────────────────────────────────

def extract_title(name_series):
    """
    Extrai o título social do nome completo (Mr, Mrs, Miss, Dr, etc.).
    Títulos raros são agrupados em 'Rare' para evitar esparsidade.
    Retorna uma Series de strings.
    """
    titles = name_series.str.extract(r",\s*([^\.]+)\.", expand=False)
    titles = titles.str.strip()

    common = {"Mr", "Mrs", "Miss", "Master", "Dr"}
    titles = titles.apply(lambda t: t if t in common else "Rare")

    dist = titles.value_counts().to_dict()
    print(f"[title] distribuição de títulos: {dist}")
    return titles


# ──────────────────────────────────────────────────────────────────────────────
# 3. ENGENHARIA DE FEATURES
# ──────────────────────────────────────────────────────────────────────────────

def engineer_features(df):
    """
    Cria features derivadas a partir das colunas brutas.
    Todas as transformações são explícitas e rastreáveis pelo noWorkflow.

    Novas features criadas:
        FamilySize  — tamanho total do grupo familiar a bordo
        IsAlone     — flag binária: passageiro sem família
        Title       — título social extraído do nome
        FareBand    — faixa de tarifa (quartis) como ordinal
        AgeBand     — faixa de idade (5 grupos) como ordinal
    """
    df = df.copy()

    # Tamanho da família
    df["FamilySize"] = df["SibSp"] + df["Parch"] + 1

    # Passageiro solo
    df["IsAlone"] = (df["FamilySize"] == 1).astype(int)

    # Título social
    df["Title"] = extract_title(df["Name"])

    # Faixa de tarifa — quartis
    df["FareBand"] = pd.qcut(
        df["Fare"], q=4, labels=[0, 1, 2, 3], duplicates="drop"
    ).astype(int)

    # Faixa de idade — usará Age já imputado (ou NaN aqui, imputado depois)
    df["AgeBand"] = pd.cut(
        df["Age"],
        bins=[0, 12, 18, 35, 60, 120],
        labels=[0, 1, 2, 3, 4],
    )

    n_alone = df["IsAlone"].sum()
    avg_family = df["FamilySize"].mean()
    print(f"[feat]  passageiros solo: {n_alone} | "
          f"tamanho médio de família: {avg_family:.2f}")

    return df


# ──────────────────────────────────────────────────────────────────────────────
# 4. LIMPEZA E IMPUTAÇÃO
#    Separada de engineer_features para ser uma ativação distinta no grafo —
#    facilita rastrear quando a estratégia de imputação muda entre trials.
# ──────────────────────────────────────────────────────────────────────────────

def clean_data(df, age_strategy, age_constant):
    """
    Trata valores ausentes e inconsistências.
    A estratégia de imputação de Age é parametrizada (median | mean | constant),
    o que gera trials distintos e rastreáveis no noWorkflow.

    Retorna o DataFrame sem valores ausentes nas colunas relevantes.
    """
    df = df.copy()

    # --- Age ---
    n_missing_age = df["Age"].isnull().sum()

    if age_strategy == "median":
        fill_age = df["Age"].median()
    elif age_strategy == "mean":
        fill_age = df["Age"].mean()
    else:                             # constant
        fill_age = age_constant

    df["Age"] = df["Age"].fillna(fill_age)

    # Recalcular AgeBand após imputação (pode ter ficado NaN antes)
    df["AgeBand"] = pd.cut(
        df["Age"],
        bins=[0, 12, 18, 35, 60, 120],
        labels=[0, 1, 2, 3, 4],
    ).astype(int)

    # --- Embarked — preenche com a moda ---
    n_missing_emb = df["Embarked"].isnull().sum()
    df["Embarked"] = df["Embarked"].fillna(df["Embarked"].mode()[0])

    # --- Fare — raramente ausente, usa mediana ---
    df["Fare"] = df["Fare"].fillna(df["Fare"].median())

    print(f"[clean] Age: {n_missing_age} ausentes → imputados com "
          f"'{age_strategy}' ({fill_age:.2f}) | "
          f"Embarked: {n_missing_emb} ausentes → imputados com moda")

    return df


# ──────────────────────────────────────────────────────────────────────────────
# 5. CODIFICAÇÃO DE VARIÁVEIS CATEGÓRICAS (ONE-HOT)
# ──────────────────────────────────────────────────────────────────────────────

def encode_categoricals(df):
    """
    Aplica One-Hot Encoding em variáveis nominais (Sex, Embarked, Title)
    e descarta colunas não usadas no modelo.

    Retorna (X, y) prontos para o treino.
    """
    df = df.copy()

    # Colunas descartadas — sem valor preditivo direto
    drop_cols = ["PassengerId", "Name", "Ticket", "Cabin",
                 "SibSp", "Parch", "Fare"]   # Fare substituída por FareBand
    df = df.drop(columns=[c for c in drop_cols if c in df.columns])

    # One-hot nas nominais
    nominal_cols = ["Sex", "Embarked", "Title"]
    df = pd.get_dummies(df, columns=nominal_cols, drop_first=False)

    # Separar label
    y = df["Survived"].values
    X = df.drop(columns=["Survived"])

    feature_names = list(X.columns)
    print(f"[encode] {len(feature_names)} features finais: {feature_names}")

    return X, y, feature_names


# ──────────────────────────────────────────────────────────────────────────────
# 6. DIVISÃO TREINO / TESTE
# ──────────────────────────────────────────────────────────────────────────────

def split_data(X, y, test_size, random_state):
    """
    Divide os dados em treino e teste estratificado pelo label.
    Retorna (X_train, X_test, y_train, y_test).
    """
    X_train, X_test, y_train, y_test = train_test_split(
        X, y,
        test_size=test_size,
        random_state=random_state,
        stratify=y,
    )
    print(f"[split] treino: {len(X_train)} | teste: {len(X_test)} | "
          f"positivos no treino: {y_train.mean():.1%}")

    return X_train, X_test, y_train, y_test


# ──────────────────────────────────────────────────────────────────────────────
# 7. TREINO DO MODELO
# ──────────────────────────────────────────────────────────────────────────────

def train_model(X_train, y_train, n_estimators, max_depth,
                min_samples_split, random_state):
    """
    Treina um RandomForestClassifier com os hiperparâmetros do trial.
    Os parâmetros são rastreados pelo noWorkflow como argumentos desta ativação.
    Retorna o modelo treinado.
    """
    model = RandomForestClassifier(
        n_estimators=n_estimators,
        max_depth=max_depth,
        min_samples_split=min_samples_split,
        random_state=random_state,
        n_jobs=-1,
    )
    model.fit(X_train, y_train)

    print(f"[train] RandomForest | n_estimators={n_estimators} | "
          f"max_depth={max_depth} | min_samples_split={min_samples_split}")

    return model


# ──────────────────────────────────────────────────────────────────────────────
# 8. VALIDAÇÃO CRUZADA
#    Separada do treino para aparecer como ativação distinta.
# ──────────────────────────────────────────────────────────────────────────────

def cross_validate_model(model, X_train, y_train, random_state):
    """
    Executa validação cruzada estratificada (5-fold) no conjunto de treino.
    Retorna dicionário com média e desvio padrão do AUC-ROC.
    """
    cv_scores = cross_val_score(
        model, X_train, y_train,
        cv=5, scoring="roc_auc", n_jobs=-1,
    )
    result = {
        "cv_auc_mean": round(float(cv_scores.mean()), 4),
        "cv_auc_std":  round(float(cv_scores.std()),  4),
    }
    print(f"[cv]    AUC-ROC 5-fold: {result['cv_auc_mean']:.4f} "
          f"± {result['cv_auc_std']:.4f}")

    return result


# ──────────────────────────────────────────────────────────────────────────────
# 9. AVALIAÇÃO NO CONJUNTO DE TESTE
# ──────────────────────────────────────────────────────────────────────────────

def evaluate_model(model, X_test, y_test):
    """
    Avalia o modelo treinado no conjunto de teste.
    Retorna dicionário com as métricas principais.
    """
    y_pred  = model.predict(X_test)
    y_proba = model.predict_proba(X_test)[:, 1]

    metrics = {
        "accuracy":  round(float(accuracy_score(y_test, y_pred)),   4),
        "f1_score":  round(float(f1_score(y_test, y_pred)),         4),
        "roc_auc":   round(float(roc_auc_score(y_test, y_proba)),   4),
    }

    print(f"[eval]  accuracy={metrics['accuracy']:.4f} | "
          f"f1={metrics['f1_score']:.4f} | "
          f"auc={metrics['roc_auc']:.4f}")
    print(classification_report(y_test, y_pred,
                                 target_names=["Não sobreviveu", "Sobreviveu"]))

    return metrics


# ──────────────────────────────────────────────────────────────────────────────
# 10. IMPORTÂNCIA VIA SHAP
#
#     NOTA PARA O PAINEL noWorkflow:
#     O noWorkflow captura esta função como uma ativação com entrada (model, X)
#     e saída (dicionário de importâncias). O que acontece *dentro* do SHAP
#     (C++/Cython) é invisível ao rastreador — apenas a chamada de superfície
#     é registrada. O valor de retorno desta função, porém, é totalmente
#     rastreável e comparável entre trials.
# ──────────────────────────────────────────────────────────────────────────────

def compute_shap_importance(model, X_train, feature_names):
    """
    Calcula importância de features via SHAP TreeExplainer.
    Retorna dicionário {feature: mean_abs_shap} ordenado por importância.

    AVISO: o interior do SHAP não é rastreado pelo noWorkflow (execução em C++).
    O valor de retorno desta função, no entanto, é capturado normalmente.
    """
    explainer   = shap.TreeExplainer(model)
    shap_values = explainer.shap_values(X_train)

    # Para classificação binária, shap_values pode ser lista [classe0, classe1]
    # ou array 3D (n_samples, n_features, n_classes)
    if isinstance(shap_values, list):
        shap_matrix = np.array(shap_values[1])
    elif shap_values.ndim == 3:
        shap_matrix = shap_values[:, :, 1]
    else:
        shap_matrix = shap_values

    mean_abs = np.abs(shap_matrix).mean(axis=0)
    importance = {
        feature_names[i]: round(float(mean_abs[i]), 6)
        for i in range(len(feature_names))
    }
    importance = dict(sorted(importance.items(),
                              key=lambda x: x[1], reverse=True))

    top5 = list(importance.items())[:5]
    print(f"[shap]  top-5 features: "
          + " | ".join(f"{k}={v:.4f}" for k, v in top5))

    return importance


# ──────────────────────────────────────────────────────────────────────────────
# 11. PERSISTÊNCIA DE RESULTADOS
# ──────────────────────────────────────────────────────────────────────────────

def save_results(metrics, cv_results, shap_importance,
                 feature_names, model, output_dir):
    """
    Salva métricas em CSV e gera gráfico de importância SHAP.
    Retorna o caminho do arquivo de métricas gerado.
    """
    os.makedirs(output_dir, exist_ok=True)

    # Métricas em CSV
    metrics_path = os.path.join(output_dir, "metrics.csv")
    all_metrics = {**metrics, **cv_results}
    pd.DataFrame([all_metrics]).to_csv(metrics_path, index=False)

    # Gráfico de importância SHAP
    try:
        chart_path = os.path.join(output_dir, "shap_importance.png")
        features_sorted = list(shap_importance.keys())[:10]
        values_sorted   = [shap_importance[f] for f in features_sorted]
    except Exception:
        # Se SHAP falhou, gera gráfico vazio com mensagem de erro
        chart_path = os.path.join(output_dir, "shap_importance_error.png")
        features_sorted = ["SHAP não disponível"]
        values_sorted = [0]

    fig, ax = plt.subplots(figsize=(8, 5))
    ax.barh(features_sorted[::-1], values_sorted[::-1], color="steelblue")
    ax.set_xlabel("Mean |SHAP value|")
    ax.set_title("Top-10 Features por Importância SHAP")
    plt.tight_layout()
    fig.savefig(chart_path, dpi=120)
    plt.close(fig)

    print(f"[save]  métricas → {metrics_path} | gráfico → {chart_path}")
    return metrics_path


# ──────────────────────────────────────────────────────────────────────────────
# MAIN — orquestra o pipeline na ordem correta
# ──────────────────────────────────────────────────────────────────────────────

def main():
    args = parse_args()

    print("=" * 60)
    print("TITANIC — Pipeline de Classificação")
    print(f"  age_strategy   = {args.age_strategy}")
    print(f"  n_estimators   = {args.n_estimators}")
    print(f"  max_depth      = {args.max_depth}")
    print(f"  min_samples_split = {args.min_samples_split}")
    print(f"  random_state   = {args.random_state}")
    print("=" * 60)

    # 1. Carga
    df_raw = load_data(args.data_path)

    # 2. Engenharia de features (antes da imputação — Age ainda pode ter NaN)
    df_feat = engineer_features(df_raw)

    # 3. Limpeza e imputação (parâmetro que varia entre trials)
    df_clean = clean_data(df_feat, args.age_strategy, args.age_constant)

    # 4. Codificação categórica → X, y prontos
    X, y, feature_names = encode_categoricals(df_clean)

    # 5. Divisão treino/teste
    X_train, X_test, y_train, y_test = split_data(
        X, y, args.test_size, args.random_state
    )

    # 6. Treino
    model = train_model(
        X_train, y_train,
        args.n_estimators, args.max_depth,
        args.min_samples_split, args.random_state,
    )

    # 7. Validação cruzada
    cv_results = cross_validate_model(model, X_train, y_train, args.random_state)

    # 8. Avaliação no teste
    metrics = evaluate_model(model, X_test, y_test)

    # 9. Importância SHAP
    # shap_importance = compute_shap_importance(model, X_train, feature_names)

    # 10. Salvar resultados
    save_results(metrics, cv_results, None,
                 feature_names, model, args.output_dir)

    print("=" * 60)
    print("CONCLUÍDO")
    print("=" * 60)
    return metrics


if __name__ == "__main__":
    main()
