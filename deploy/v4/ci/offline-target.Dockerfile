FROM ubuntu:24.04

ENV container=docker
STOPSIGNAL SIGRTMIN+3

# Keep the target to the init system and TLS trust store. The offline bundle must provide the node packages,
# runtimes and Docker engine used by the actual install.
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      ca-certificates systemd systemd-sysv \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists/*

VOLUME ["/sys/fs/cgroup"]
CMD ["/sbin/init"]
