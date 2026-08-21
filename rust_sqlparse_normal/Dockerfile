# Stable build environment.
# CentOS 7 ships GLIBC 2.17, so build artifacts run on systems with GLIBC >= 2.17.
FROM centos:7

WORKDIR /app

RUN curl -o /etc/yum.repos.d/CentOS-Base.repo https://mirrors.aliyun.com/repo/Centos-7.repo && \
    yum clean all && \
    yum makecache

# Install gcc and required build tools.
RUN yum install -y gcc make

# Install Rust.
ENV RUSTUP_HOME=/usr/local/rustup \
    CARGO_HOME=/usr/local/cargo \
    PATH=/usr/local/cargo/bin:$PATH

# Use the configured Rustup mirror.
ENV RUSTUP_DIST_SERVER=https://rsproxy.cn
ENV RUSTUP_UPDATE_ROOT=https://rsproxy.cn/rustup

RUN curl --proto '=https' --tlsv1.2 -sSf https://rsproxy.cn/rustup-init.sh | sh -s -- -y --default-toolchain 1.75

# Keep the container running.
CMD ["tail", "-f", "/dev/null"]
