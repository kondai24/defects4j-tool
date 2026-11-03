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
    openjdk-21-jdk \
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
# claude-agent-sdk: Claude Agent SDK (fix, FIX_PROVIDER=claude。Claude Code CLI を同梱)
# openai-codex: OpenAI Codex SDK (fix, FIX_PROVIDER=codex。Codex CLI を同梱)
RUN pip install --no-cache-dir \
    networkx \
    "pydot==1.4.2" \
    "pyparsing<3" \
    "tree-sitter==0.21.3" \
    "tree-sitter-languages==1.10.2" \
    "claude-agent-sdk==0.2.166" \
    "openai-codex==0.162.1" \
    pyyaml

# Keep Defects4J on Java 11 and select Java 21 only for Joern.
# Stable paths work on both amd64 and arm64 images.
RUN ln -s /usr/lib/jvm/java-11-openjdk-$(dpkg --print-architecture) /opt/java11 && \
    ln -s /usr/lib/jvm/java-21-openjdk-$(dpkg --print-architecture) /opt/java21
ENV JAVA_HOME=/opt/java11
ENV JOERN_JAVA_HOME=/opt/java21
ENV PATH="${JAVA_HOME}/bin:${PATH}"

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
# Pin Joern together with its Java requirement for reproducible rebuilds.
ARG JOERN_VERSION=4.0.665
RUN case "$(dpkg --print-architecture)" in \
      amd64) joern_arch=x86_64 ;; \
      arm64) joern_arch=arm64 ;; \
      *) echo "Unsupported Joern architecture" >&2; exit 1 ;; \
    esac && \
    curl -fL -o joern-cli.zip "https://github.com/joernio/joern/releases/download/v${JOERN_VERSION}/joern-cli-linux-${joern_arch}.zip" && \
    unzip joern-cli.zip && \
    rm joern-cli.zip && \
    mv joern-cli joern

# JoernをPATHに追加
ENV PATH="/opt/joern:${PATH}"

# Detect Java/Joern incompatibility while building the image.
RUN JAVA_HOME=${JOERN_JAVA_HOME} PATH="${JOERN_JAVA_HOME}/bin:${PATH}" joern-parse --help && \
    JAVA_HOME=${JOERN_JAVA_HOME} PATH="${JOERN_JAVA_HOME}/bin:${PATH}" joern-export --help

#############################################################################
# Setup JBMC (verify ステップ)
#############################################################################
# CBMC の公式 .deb に JBMC と core-models.jar が含まれる。版とハッシュを固定する。
ARG CBMC_VERSION=6.11.0
ARG CBMC_SHA256_AMD64=b3721aa541038384d7801ea3aeabbcddc3e8845ac8f1cbff637cf8dec7481ac8
ARG CBMC_SHA256_ARM64=9eb8f41a7dcda8eac6338b8367d77831431f767691e4535209925bd85e315ae7
RUN case "$(dpkg --print-architecture)" in \
      amd64) cbmc_deb="ubuntu-24.04-cbmc-${CBMC_VERSION}-Linux.deb"; cbmc_sha="${CBMC_SHA256_AMD64}" ;; \
      arm64) cbmc_deb="ubuntu-24.04-arm64-cbmc-${CBMC_VERSION}-Linux.deb"; cbmc_sha="${CBMC_SHA256_ARM64}" ;; \
      *) echo "Unsupported CBMC architecture" >&2; exit 1 ;; \
    esac && \
    curl -fL -o /tmp/cbmc.deb "https://github.com/diffblue/cbmc/releases/download/cbmc-${CBMC_VERSION}/${cbmc_deb}" && \
    echo "${cbmc_sha}  /tmp/cbmc.deb" | sha256sum -c - && \
    apt-get update -y && apt-get install -y /tmp/cbmc.deb && \
    rm -rf /tmp/cbmc.deb /var/lib/apt/lists/* && \
    jbmc --version && test -f /usr/lib/core-models.jar
ENV JBMC_CORE_MODELS=/usr/lib/core-models.jar

# 作業ディレクトリに戻る
WORKDIR /workspace
