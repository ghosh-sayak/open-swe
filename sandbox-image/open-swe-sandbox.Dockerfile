FROM daytonaio/sandbox:0.5.0-slim

USER root

RUN apt-get update && \
    apt-get install -y curl gpg && \
    curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | \
    dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg && \
    chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | \
    tee /etc/apt/sources.list.d/github-cli.list > /dev/null && \
    apt-get update && \
    apt-get install -y gh git && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# The agent always invokes `GH_TOKEN=dummy gh` (a LangSmith-proxy convention). On
# providers without that proxy, the dummy token would override the real one in
# ~/.config/gh/hosts.yml and 401. /usr/local/bin precedes /usr/bin in PATH, so this
# wrapper shadows the real gh (kept at /usr/bin/gh) and strips the dummy token,
# letting gh fall back to hosts.yml. No recursion, real binary untouched.
RUN printf '#!/bin/sh\nunset GH_TOKEN GITHUB_TOKEN\nexec /usr/bin/gh "$@"\n' > /usr/local/bin/gh && \
    chmod +x /usr/local/bin/gh
