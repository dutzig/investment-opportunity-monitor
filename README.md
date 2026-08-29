# investment-opportunity-monitor

Plugin do Claude Code com uma skill de monitoramento de oportunidades de
investimento, somente-leitura, cobrindo múltiplas classes de ativo por
configuração (não por código duplicado).

- Skill: [`skills/investment-opportunity-monitor/SKILL.md`](skills/investment-opportunity-monitor/SKILL.md)
- Classes implementadas e testadas contra a fonte real: **DeFi** (DefiLlama),
  **renda fixa / Tesouro Direto** (Tesouro Transparente) e **ações B3**
  (Yahoo Finance, por watchlist) — ver
  [`docs/adding-asset-class.md`](skills/investment-opportunity-monitor/docs/adding-asset-class.md)
  pra adicionar outras (ex: ações de outros mercados)
- Deploy em VPS: [`docs/deploy-vps.md`](skills/investment-opportunity-monitor/docs/deploy-vps.md) — ainda não executado, só documentado

## Instalar

```bash
git clone <url-deste-repo> ~/.claude/plugins/investment-opportunity-monitor
```

(ou adicione este repositório como plugin via mecanismo de plugins do
Claude Code, se preferir não clonar manualmente)

## Rodar

```bash
cd skills/investment-opportunity-monitor
python3 scripts/monitor.py --asset-class defi --top 15
```

Segurança: esta skill nunca conecta wallet, nunca executa ordens, nunca
guarda credenciais, e nunca dá recomendação personalizada de investimento —
ela só busca dados públicos e calcula um score de risco comparativo e
transparente. Ver a seção "Regras de segurança" em SKILL.md.
