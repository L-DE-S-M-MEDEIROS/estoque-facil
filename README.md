# Estoque Fácil

Aplicativo web responsivo para controle manual de estoque. Os dados são armazenados no navegador e podem ser exportados ou restaurados pelas configurações.

## Recursos

- Cadastro de produtos sem SKU ou código de barras
- Foto opcional, categoria, unidade, custo e estoque mínimo
- Entradas, saídas e ajustes com data editável
- Inventário integrado às movimentações
- Histórico completo e filtros por operação
- Backup e restauração em JSON
- Consulta de novas versões publicadas no GitHub

## Desenvolvimento

```bash
pnpm install
pnpm dev
```

Abra `http://localhost:3000`.

## Atualizações

O botão **Buscar atualização** consulta a versão mais recente em Releases. Para publicar uma atualização, ajuste `CURRENT_VERSION`, crie uma tag `vX.Y.Z` e publique uma GitHub Release.
