FROM python:3.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends smartmontools gcc libc6-dev \
    && rm -rf /var/lib/apt/lists/*

COPY it87ctl.c /tmp/it87ctl.c
RUN gcc -O2 -Wall -Wextra -Werror -o /usr/local/bin/it87ctl /tmp/it87ctl.c \
    && rm /tmp/it87ctl.c \
    && apt-get purge -y gcc libc6-dev \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

COPY frontpanel.py /usr/local/lib/asustor-frontpanel/frontpanel.py
ENTRYPOINT ["python3", "/usr/local/lib/asustor-frontpanel/frontpanel.py"]
