FROM ubuntu:24.04

#############################################################################
# Requirements
#############################################################################

ENV DEBIAN_FRONTEND=noninteractive

RUN \
  apt-get update -y && \
  apt-get install -y software-properties-common && \
  add-apt-repository -y ppa:deadsnakes/ppa && \
  apt-get update -y && \
  apt-get install -y \
    openjdk-11-jdk \
    git \
    build-essential \
    subversion \
    perl \
    curl \
    unzip \
    cpanminus \
    make \
    graphviz \
    python3.12 \
    python3.12-venv \
    python3.12-dev \
  && \
  rm -rf /var/lib/apt/lists/*

#############################################################################
# Python venv (PEP 668 回避 + Python 3.12 固定)
#############################################################################

ENV VENV_PATH=/opt/venv
RUN python3.12 -m venv ${VENV_PATH}
ENV PATH="${VENV_PATH}/bin:${PATH}"

RUN pip install --no-cache-dir -U pip

# Python libraries
# networkx/pydot/pyparsing: CFG_PDGモードの .dot 読み取り (networkx.nx_pydot.read_dot) に必要
# tree-sitter-languages: ASTモードのS式AST生成に必要
# github-copilot-sdk: GitHub Copilot SDK (Python)
RUN pip install --no-cache-dir \
    networkx \
    "pydot==1.4.2" \
    "pyparsing<3" \
    "tree-sitter==0.21.3" \
    "tree-sitter-languages==1.10.2" \
    "github-copilot-sdk==0.1.18" \
    pyyaml

RUN curl -fsSL https://gh.io/copilot-install | bash

# Java version
ENV JAVA_HOME=/usr/lib/jvm/java-11-openjdk-amd64

# Timezone
ENV TZ=America/Los_Angeles
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

#############################################################################
# Setup Defects4J
#############################################################################

# ----------- Step 1. Clone defects4j from github --------------
WORKDIR /
RUN git clone https://github.com/rjust/defects4j.git defects4j

# ----------- Step 2. Initialize Defects4J ---------------------
WORKDIR /defects4j
RUN cpanm --installdeps .
RUN ./init.sh

# ----------- Step 3. Add Defects4J's executables to PATH: ------
ENV PATH="/defects4j/framework/bin:${PATH}"

#############################################################################
# Setup Joern
#############################################################################
WORKDIR /opt
# 最新版のJoern CLIをダウンロードしてインストール
RUN curl -L -o joern-cli.zip https://github.com/joernio/joern/releases/latest/download/joern-cli.zip && \
    unzip joern-cli.zip && \
    rm joern-cli.zip && \
    mv joern-cli joern

# JoernをPATHに追加
ENV PATH="/opt/joern:${PATH}"

# 作業ディレクトリに戻る
WORKDIR /workspace
