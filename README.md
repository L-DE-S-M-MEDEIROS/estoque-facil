# Estoque Bolsas Baby

Aplicativo desktop nativo para Windows, desenvolvido em Python com Tkinter e SQLite.

## Sincronização com Supabase

Na página **Configurações**, use o cartão **Supabase — cópia na nuvem** para criar ou entrar em uma conta, enviar os dados locais e restaurar a cópia remota. Produtos, movimentações, cadastros e fotos são enviados ao projeto separado `Estoque Bolsas Baby`.

- O aplicativo continua funcionando localmente sem internet.
- Cada conta acessa somente a própria cópia por meio de Row Level Security (RLS).
- A senha não é armazenada; somente a sessão de acesso fica salva neste computador.
- Antes de baixar e substituir os dados locais, o aplicativo cria um backup automático.
- A aba **Movimentações** possui rolagem vertical e permite recolher ou expandir a lista pesquisável de produtos.
- A tabela **Posição do estoque** possui barra de rolagem própria para suportar catálogos maiores.
- O botão de recolher produtos fica integrado à busca, com ícone de pesquisa ajustado para melhor legibilidade.

## Recursos

- Cadastro de produtos sem SKU ou código de barras
- Organização por Categoria, Grupo/modelo e Variação, mantendo estoque independente
- Cadastro separado de grupos, com reaproveitamento dos grupos salvos ao criar ou editar produtos
- Foto opcional, categoria, unidade e estoque mínimo
- Aba **Estoque atual** com saldo, situação e confiança; a última contagem fica somente na aba **Contagem**
- Estoque atual agrupado com separadores visuais, produtos em ordem alfabética e saldos coloridos por quantidade
- Estoque negativo permitido, com aviso persistente por produto e saldo destacado em vinho escuro
- Aba **Contagem** no formato de check-in da planilha, com quantidade física, data e responsável
- Última contagem em tempo relativo e colorido: hoje, dias, semanas, meses ou anos atrás
- Aba **Cadastro** com gerenciadores separados e ilustrados para Usuários, Operações, Grupos e Produtos
- Operações padrão e personalizadas visíveis no mesmo gerenciador, com edição e remoção segura
- Movimentações em lote no estilo carrinho: vários produtos, uma operação, data, observação e usuário responsável
- Revisão, edição e remoção dos itens antes de salvar o conjunto
- Pesquisa rápida na movimentação por parte do nome, grupo, variação ou categoria, ignorando diferenças de acento e maiúsculas
- Índice de confiança calculado pelos dias, frequência e volume movimentado desde a última contagem
- Medidores de confiança: vermelho de 0% a 40%, amarelo de 41% a 60%, verde de 61% a 89% e azul de 90% a 100%
- Mini velocímetros substituem os percentuais nas colunas de confiança das tabelas
- Linhas divisórias finas e adaptadas ao tema em todas as tabelas do aplicativo
- Entradas, saídas, ajustes e inventário com data editável
- Edição e exclusão de movimentações com recálculo seguro dos saldos
- Campo de data mascarado em `dd/mm/aa` e calendário para seleção com o mouse
- Histórico completo de movimentações
- Backup e restauração do banco SQLite
- A exclusão geral de todos os dados não fica disponível na interface
- Consulta de atualizações pelo GitHub
- Interface adaptável a Full HD, 2K e 4K, respeitando a escala de DPI do Windows
- Temas Light off-white e Dark grafite com azul neon
- Preferências da interface salvas localmente por usuário do Windows, separadas da sessão e dos dados sincronizados pelo Supabase
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
