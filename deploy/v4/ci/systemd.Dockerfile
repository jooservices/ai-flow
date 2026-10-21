FROM ubuntu:24.04

ENV container=docker
STOPSIGNAL SIGRTMIN+3

RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      ca-certificates curl git jq python3 python3-pip systemd systemd-sysv tar \
 && python3 -m pip install --break-system-packages --no-cache-dir uv==0.13.4 \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists/*

VOLUME ["/sys/fs/cgroup"]
CMD ["/sbin/init"]
