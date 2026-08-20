# Estoque Bolsas Baby

Aplicativo desktop nativo para Windows, desenvolvido em Python com Tkinter e SQLite.

## Recursos

- Cadastro de produtos sem SKU ou código de barras
- Organização por Categoria, Grupo/modelo e Variação, mantendo estoque independente
- Foto opcional, categoria, unidade, custo e estoque mínimo
- Aba **Estoque atual** com saldo, situação e valor estimado
- Entradas, saídas, ajustes e inventário com data editável
- Campo de data mascarado em `dd/mm/aa` e calendário para seleção com o mouse
- Histórico completo de movimentações
- Backup e restauração do banco SQLite
- Consulta de atualizações pelo GitHub
- Interface adaptável a Full HD, 2K e 4K, respeitando a escala de DPI do Windows
- Temas Light off-white e Dark grafite com azul neon
- Componentes arredondados, espaçamento amplo e hierarquia tipográfica moderna
- Ícones próprios em alta resolução com redução antialiasada
- Navegação priorizando Estoque atual, Movimentações e Produtos

## Executar pelo código

```powershell
python -m pip install -r requirements.txt
python app.py
```

## Gerar o executável

```powershell
.\build-python.ps1
```

Os dados são guardados em `%LOCALAPPDATA%\EstoqueFacil\estoque.db`.
