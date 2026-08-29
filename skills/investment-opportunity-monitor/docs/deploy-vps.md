# Deploy na VPS

Este documento é a referência para quando o acesso SSH à VPS estiver
disponível. **Nenhum destes comandos deve ser executado numa VPS real sem
antes explicar o plano completo passo a passo e obter confirmação do
usuário** — isso vale mesmo que o usuário já tenha aprovado o deploy em
geral; cada etapa que mexe em usuário do sistema, pacotes, portas ou nginx
precisa de confirmação própria.

## Rodando localmente vs. na VPS

Localmente (o que já está validado nesta skill): rodar
`python3 scripts/monitor.py --asset-class defi` manualmente, quando quiser.
Sem systemd, sem nginx, sem usuário dedicado — é só um script.

Na VPS, o objetivo é o mesmo script rodando sozinho em intervalos, com
histórico persistido em disco e sem expor nada publicamente sem
autenticação. As seções abaixo cobrem cada peça.

## 1. Usuário Linux dedicado, sem root

```bash
sudo useradd --system --create-home --shell /usr/sbin/nologin investmon
sudo mkdir -p /opt/investment-opportunity-monitor
sudo chown investmon:investmon /opt/investment-opportunity-monitor
```

Todo o resto (clone do repo, venv, execução) roda como esse usuário
(`sudo -u investmon ...`), nunca como root.

## 2. Ambiente virtual isolado

```bash
sudo -u investmon git clone <url-do-repo> /opt/investment-opportunity-monitor
cd /opt/investment-opportunity-monitor/skills/investment-opportunity-monitor
sudo -u investmon python3 -m venv .venv
sudo -u investmon .venv/bin/pip install -r scripts/requirements.txt
```

`scripts/requirements.txt` hoje só tem dependências da stdlib para DeFi
(nenhuma dependência externa). Quando `stocks` e `fixed-income` forem
implementados, dependências adicionais (se houver) entram nesse arquivo.

## 3. Execução recorrente via systemd timer (preferível a cron)

`/etc/systemd/system/investmon-defi.service`:
```ini
[Unit]
Description=investment-opportunity-monitor (defi)

[Service]
Type=oneshot
User=investmon
WorkingDirectory=/opt/investment-opportunity-monitor/skills/investment-opportunity-monitor
ExecStart=/opt/investment-opportunity-monitor/skills/investment-opportunity-monitor/.venv/bin/python3 scripts/monitor.py --asset-class defi --top 50 --json
StandardOutput=append:/var/log/investmon/defi.log
```

`/etc/systemd/system/investmon-defi.timer`:
```ini
[Unit]
Description=Roda investmon-defi a cada 6 horas

[Timer]
OnBootSec=5min
OnUnitActiveSec=6h
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
sudo mkdir -p /var/log/investmon && sudo chown investmon:investmon /var/log/investmon
sudo systemctl daemon-reload
sudo systemctl enable --now investmon-defi.timer
```

Repita service+timer por classe de ativo quando `stocks` e `fixed-income`
estiverem implementados (arquivos separados, mesmo padrão).

## 4. Histórico persistido + logrotate

O histórico já fica em SQLite dentro de `data/<classe>_history.db` — isso
persiste naturalmente em disco, sem configuração extra. Para o log de
execução (`/var/log/investmon/*.log`), configure logrotate:

`/etc/logrotate.d/investmon`:
```
/var/log/investmon/*.log {
    weekly
    rotate 8
    compress
    missingok
    notifempty
}
```

## 5. Visualização remota (opcional)

Só necessário se você quiser ver os dados de fora da VPS sem SSH direto.
**Nunca expor porta sem autenticação.** Duas opções, em ordem de
preferência:

**a) Túnel SSH (mais simples, sem mudar nada na VPS):**
```bash
ssh -L 8080:localhost:8080 usuario@vps
```
e rodar localmente na porta 8080 algo que sirva os dados (ex: um pequeno
`http.server` apontando pra um export do SQLite) — nada fica exposto na
internet.

**b) nginx + autenticação básica**, se precisar de acesso via navegador
sem túnel:
```bash
sudo apt install nginx apache2-utils
sudo htpasswd -c /etc/nginx/.htpasswd investmon
```
Configurar o `server` block do nginx com `auth_basic` apontando pra esse
arquivo, fazendo proxy pra um serviço local que nunca escuta em `0.0.0.0`
diretamente. Se houver domínio, adicionar HTTPS com `certbot --nginx`.

VPN (Tailscale/WireGuard) é uma alternativa a (b) se você já usa algo assim
— evita expor a porta 80/443 publicamente.

## Checklist antes de qualquer alteração de sistema/rede real

Antes de rodar qualquer comando desta página numa VPS de verdade:

1. Explicar o plano completo (o que vai ser criado/instalado/aberto)
2. Pedir confirmação do usuário passo a passo — não rodar tudo de uma vez
3. Preferir a opção reversível quando houver escolha (ex: túnel SSH antes
   de nginx+htpasswd, já que não abre porta nova)
4. Nunca abrir porta publicamente sem autenticação, mesmo temporariamente
