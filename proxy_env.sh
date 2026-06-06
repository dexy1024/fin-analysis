#!/usr/bin/env bash
# Clash Verge 等本地代理：yfinance 等境外源需要走代理；
# 东财/新浪等国内源走直连，避免 HTTPS 经代理时出现 ProxyError。
#
# 用法（在其它脚本中）:
#   source "$(dirname "$0")/proxy_env.sh"
# 自定义端口:
#   PROXY_PORT=7890 source proxy_env.sh

PROXY_PORT="${PROXY_PORT:-7897}"
PROXY_URL="http://127.0.0.1:${PROXY_PORT}"

export HTTP_PROXY="${PROXY_URL}"
export HTTPS_PROXY="${PROXY_URL}"
export http_proxy="${PROXY_URL}"
export https_proxy="${PROXY_URL}"

export NO_PROXY="localhost,127.0.0.1,::1,.local,eastmoney.com,.eastmoney.com,sina.com.cn,.sina.com.cn,sinaimg.cn,.sinaimg.cn,163.com,.163.com,qq.com,.qq.com,finance.qq.com"
export no_proxy="${NO_PROXY}"
