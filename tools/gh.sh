#!/bin/sh
# gh 包装：先注入 GITHUB_TOKEN 再执行真正的 gh。
# 交互式 shell 不会自动带上 token（它在 /etc/dabai/secrets.env，root:wxf 0640），
# 所以裸 gh 会报 "not logged into any GitHub hosts"。
if [ -z "$GITHUB_TOKEN" ] && [ -z "$GH_TOKEN" ]; then
    f=/etc/dabai/secrets.env
    [ -r "$f" ] || f="$HOME/.config/dabai/secrets.env"
    if [ -r "$f" ]; then
        # 文件里的值是带引号的（GITHUB_TOKEN='ghp_...'），systemd 会剥引号，这里得自己剥
        GITHUB_TOKEN=$(grep -m1 '^GITHUB_TOKEN=' "$f" | cut -d= -f2- | tr -d '\042\047\015')
        export GITHUB_TOKEN
    fi
fi
exec /usr/bin/gh "$@"
