# Estoque Bolsas Baby

Aplicativo desktop nativo para Windows, desenvolvido em Python com Tkinter e SQLite.

## Sincronização com Supabase

Na página **Configurações**, use o cartão **Supabase — cópia na nuvem** para criar ou entrar em uma conta. Todos os usuários autenticados neste aplicativo compartilham o mesmo estoque, e produtos, movimentações, cadastros e fotos são sincronizados automaticamente no projeto separado `Estoque Bolsas Baby`.

- O aplicativo continua funcionando localmente sem internet.
- Todas as contas autenticadas do aplicativo acessam o estoque compartilhado; usuários anônimos continuam bloqueados por Row Level Security (RLS).
- Ao entrar, ao alterar dados e a cada 20 segundos, o aplicativo compara a cópia local com a nuvem e atualiza os outros computadores.
- A senha não é armazenada; somente a sessão de acesso fica salva neste computador.
- Antes de baixar e substituir os dados locais, o aplicativo cria um backup automático.
- A aba **Movimentações** possui rolagem suave e isolada: listas e tabelas internas não arrastam a página ao mesmo tempo.
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
- Aba **Contagem** no formato de check-in da planilha, com busca intuitiva por produto, grupo ou variação, lista resumida de resultados, quantidade física, data e responsável selecionado entre os usuários ativos cadastrados
- Na lista da Contagem, um clique prepara o produto para conferência e a ação **Editar produto** abre o cadastro do item selecionado
- Última contagem em tempo relativo e colorido: hoje, dias, semanas, meses ou anos atrás
- Aba **Cadastro** com gerenciadores separados e ilustrados para Usuários, Operações, Grupos e Produtos
- Operações padrão e personalizadas visíveis no mesmo gerenciador, com edição e remoção segura
- Movimentações em lote no estilo carrinho: vários produtos, uma operação, data, observação e usuário responsável
- Revisão, edição e remoção dos itens antes de salvar o conjunto
- Soma em tempo real da quantidade total de itens informados na movimentação
- Páginas internas separadas para **Nova movimentação** e **Histórico**, preservando o padrão visual do aplicativo
- Histórico consolidado: cada linha representa uma movimentação fechada, com janela de detalhes dos produtos
- Edição e exclusão do conjunto completo, com recálculo seguro de todos os saldos envolvidos
- Importação de listas PDF da Shopee e do Mercado Livre usando apenas SKU e quantidade
- Leitura posicional da Lista Shopee, inclusive quando o SKU ocupa mais de uma linha
- Localização automática das páginas de lista no final de PDFs com etiquetas e notas fiscais do Mercado Livre
- Memória sincronizada dos vínculos entre cada SKU e um ou mais produtos do estoque
- Conferência por SKU e baixa consolidada por produto antes de levar a lista para Movimentações
- Gerenciador de SKUs na aba Cadastro para criar, pesquisar, alterar ou excluir vínculos futuros
- Editor de SKU responsivo com busca curta, tabela virtualizada e seleção de produtos por clique, sem criar dezenas de controles pesados
- Aba **Simulação** para montar um conjunto de entrada ou saída que mostra somente os produtos adicionados e compara, lado a lado, o estoque atual com o saldo simulado, sem registrar movimentações
- Impressão da Simulação em PDF com uma lista de separação contendo somente produto e quantidade simulada; o estoque atual e o saldo simulado permanecem exclusivos da tela do aplicativo
- Rascunho da simulação salvo somente no computador do usuário, fora da sincronização do Supabase
- Pesquisa rápida na movimentação por parte do nome, grupo, variação ou categoria, ignorando diferenças de acento e maiúsculas
- Índice de confiança calculado pelos dias, frequência e volume movimentado desde a última contagem
- Medidores de confiança: vermelho de 0% a 40%, amarelo de 41% a 60%, verde de 61% a 89% e azul de 90% a 100%
- Mini velocímetros substituem os percentuais nas colunas de confiança das tabelas
- Linhas divisórias finas e adaptadas ao tema em todas as tabelas do aplicativo
- Entradas, saídas, ajustes e inventário com data editável
- Campo de data mascarado em `dd/mm/aa` e calendário para seleção com o mouse
- Histórico completo de movimentações
- Backup e restauração do banco SQLite
- A exclusão geral de todos os dados não fica disponível na interface
- Verificação automática de atualizações ao abrir, com aviso quando houver uma nova versão
- Download seguro pelo próprio aplicativo, validação SHA-256, substituição da versão anterior e reinício automático
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
