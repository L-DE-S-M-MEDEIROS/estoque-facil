# Estoque Bolsas Baby

Aplicativo desktop nativo para Windows, desenvolvido em Python com Tkinter e SQLite.

## Recursos

- Cadastro de produtos sem SKU ou código de barras
- Organização por Categoria, Grupo/modelo e Variação, mantendo estoque independente
- Foto opcional, categoria, unidade e estoque mínimo
- Aba **Estoque atual** com saldo, situação e confiança; a última contagem fica somente na aba **Contagem**
- Estoque atual agrupado com separadores visuais, produtos em ordem alfabética e saldos coloridos por quantidade
- Aba **Contagem** no formato de check-in da planilha, com quantidade física, data e responsável
- Índice de confiança calculado pelos dias, frequência e volume movimentado desde a última contagem
- Medidores de confiança: vermelho de 0% a 40%, amarelo de 41% a 60%, verde de 61% a 89% e azul de 90% a 100%
- Mini velocímetros substituem os percentuais nas colunas de confiança das tabelas
- Entradas, saídas, ajustes e inventário com data editável
- Edição e exclusão de movimentações com recálculo seguro dos saldos
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
