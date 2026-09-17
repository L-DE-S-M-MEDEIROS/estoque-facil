# Estoque Bolsas Baby

Aplicativo desktop nativo para Windows, desenvolvido em Python com Tkinter e SQLite.

## Sincronização com Firebase

Na página **Configurações**, use o cartão **Firebase — estoque online compartilhado** para criar ou entrar na conta autorizada. Produtos, movimentações, cadastros e fotos são sincronizados automaticamente no projeto separado `Estoque Bolsas Baby` (`estoque-bolsas-baby`).

- O SQLite local é apenas um cache operacional; o Firebase Realtime Database é a fonte central para operar em qualquer computador.
- O banco exige autenticação e restringe o acesso ao e-mail autorizado; usuários anônimos permanecem bloqueados pelas regras do Firebase.
- Ao entrar, ao alterar dados e a cada 20 segundos, o aplicativo compara a cópia local com a nuvem e atualiza os outros computadores.
- A senha não é armazenada; somente a sessão de acesso fica salva neste computador.
- Antes de baixar e substituir os dados locais, o aplicativo cria um backup automático.
- A aba **Movimentações** possui rolagem suave e isolada: listas e tabelas internas não arrastam a página ao mesmo tempo.
- A tabela **Posição do estoque** possui barra de rolagem própria para suportar catálogos maiores.
- O botão de recolher produtos fica integrado à busca, com ícone de pesquisa ajustado para melhor legibilidade.

## Visualização online no Google Planilhas

O arquivo **ESTOQUE SICRONIZADO** do Google Planilhas consulta diretamente o Firebase usando a autorização Google do proprietário. Ele pode ser aberto pelo cartão **Google Planilhas — sincronização automática online** em **Configurações**.

- O Firebase e o banco local continuam sendo a fonte oficial do estoque; a planilha oferece uma visualização simples e um campo de contagem mensal.
- A primeira aba é sempre **ESTOQUE ATUAL**, protegida contra edição, com todos os produtos, seus saldos atuais e o total no final.
- Depois dela, o aplicativo cria uma aba para cada mês, com o produto no formato `GRUPO/MODELO + PRODUTO + VARIAÇÃO`.
- Cada aba possui as colunas **Produto**, **Estoque do sistema**, **Contagem**, **Diferença** e **Estoque final**, além do total do estoque final.
- Somente a coluna **Contagem** fica liberada para digitação. Uma diferença negativa aparece em vermelho e uma positiva em verde.
- A coluna **Contagem** pertence somente ao Google Planilhas: não é preenchida pelo aplicativo e não altera o estoque no Firebase.
- No mês atual, **Estoque do sistema** acompanha o mesmo saldo exibido em **ESTOQUE ATUAL**; novos produtos e movimentações aparecem automaticamente.
- Não há arquivo local, pasta OneDrive nem cliente do OneDrive obrigatório. Qualquer computador conectado à internet pode visualizar a mesma planilha.
- A planilha consulta o Firebase automaticamente a cada minuto, mesmo que este computador esteja desligado.

## Recursos

- Cadastro de produtos sem SKU ou código de barras
- Organização por Categoria, Grupo/modelo e Variação, mantendo estoque independente
- Bloqueio de produto duplicado dentro do mesmo grupo, ignorando diferenças de acentos, maiúsculas e espaços; o mesmo nome continua permitido em grupos diferentes
- Cadastro separado de grupos, com reaproveitamento dos grupos salvos ao criar ou editar produtos
- Foto opcional, categoria, unidade e estoque mínimo
- Aba **Estoque atual** com saldo, situação e confiança; a última contagem fica somente na aba **Contagem**
- Estoque atual agrupado com separadores visuais, produtos em ordem alfabética e saldos coloridos por quantidade
- Estoque negativo permitido, com aviso persistente por produto e saldo destacado em vinho escuro
- Aba **Contagem** no formato de check-in da planilha, com busca intuitiva por produto, grupo ou variação, lista resumida de resultados, quantidade física, data e responsável selecionado entre os usuários ativos cadastrados; o formulário possui rolagem própria para manter todos os campos acessíveis sem mover a lista ao lado
- Na lista da Contagem, um clique prepara o produto para conferência e a ação **Editar produto** abre o cadastro do item selecionado
- Botão **Imprimir estoque** na aba Contagem, gerando uma folha completa em PDF com Grupo, Produto, Saldo atual e um campo em branco para a conferência manual
- Última contagem em tempo relativo e colorido: hoje, dias, semanas, meses ou anos atrás
- Aba **Cadastro** com gerenciadores separados e ilustrados para Usuários, Operações, Grupos e Produtos
- Operações padrão e personalizadas visíveis no mesmo gerenciador, com edição e remoção segura
- Movimentações em lote no estilo carrinho: vários produtos, uma operação, data, observação e usuário responsável
- Aba **Montagem / Desmontagem** para converter kits de 2, 4 e 5 peças na proporção de 1 para 1; a origem perde unidades e o destino recebe unidades dentro da mesma movimentação fechada
- Seleção inteligente que mostra somente kits menores da mesma família, cor e variação, impedindo combinações incompatíveis
- Revisão, edição e remoção dos itens antes de salvar o conjunto
- Soma em tempo real da quantidade total de itens informados na movimentação
- Páginas internas separadas para **Nova movimentação** e **Histórico**, preservando o padrão visual do aplicativo
- Histórico consolidado: cada linha representa uma movimentação fechada, com janela de detalhes dos produtos
- Filtro do histórico por data inicial e final, combinável com a operação e com calendário em ambos os campos
- Nos detalhes do histórico, cada produto mostra Estoque antes, Alteração e Saldo após para facilitar a conferência da movimentação
- Edição e exclusão do conjunto completo, com recálculo seguro de todos os saldos envolvidos
- Importação de listas PDF da Shopee e do Mercado Livre usando apenas SKU e quantidade
- Leitura posicional da Lista Shopee, inclusive quando o SKU ocupa mais de uma linha
- Localização automática das páginas de lista no final de PDFs com etiquetas e notas fiscais do Mercado Livre
- Memória sincronizada dos vínculos entre cada SKU e um ou mais produtos do estoque
- Janelas de vínculo e conferência de SKU centralizadas geometricamente no monitor, inclusive com escala do Windows acima de 100%, e ajustadas à área disponível
- Conferência por SKU e baixa consolidada por produto antes de levar a lista para Movimentações
- Gerenciador de SKUs na aba Cadastro para criar, pesquisar, alterar ou excluir vínculos futuros
- Editor de SKU responsivo com busca curta, tabela virtualizada e seleção de produtos por clique, sem criar dezenas de controles pesados
- Aba **Simulação** para montar um conjunto de entrada ou saída que mostra somente os produtos adicionados e compara, lado a lado, o estoque atual com o saldo simulado, sem registrar movimentações
- Impressão da Simulação em PDF com uma lista de separação contendo somente produto e quantidade simulada; o estoque atual e o saldo simulado permanecem exclusivos da tela do aplicativo
- Rascunho da simulação salvo somente no computador do usuário, fora da sincronização do Firebase
- Pesquisa rápida na movimentação por parte do nome, grupo, variação ou categoria, ignorando diferenças de acento e maiúsculas
- Aba **Defeito / Devolução** para retirar ou devolver uma unidade rapidamente, com a mesma busca simplificada e limpeza automática após o registro
- Pesquisas de produto otimizadas com resultado resumido, cache seguro e atualização automática após alterações locais ou sincronizadas
- Índice de confiança calculado pelos dias, frequência e volume movimentado desde a última contagem
- Medidores de confiança: vermelho de 0% a 40%, amarelo de 41% a 60%, verde de 61% a 89% e azul de 90% a 100%
- Mini velocímetros substituem os percentuais nas colunas de confiança das tabelas
- Linhas divisórias finas e adaptadas ao tema em todas as tabelas do aplicativo
- Entradas, saídas, ajustes e inventário com data editável
- Campo de data mascarado em `dd/mm/aa` e calendário para seleção com o mouse
- Histórico completo de movimentações
- Backup e restauração do banco SQLite
- Validação de integridade antes de restaurar backups ou aceitar dados e fotos recebidos da nuvem
- A exclusão geral de todos os dados não fica disponível na interface
- Verificação automática de atualizações ao abrir, com aviso quando houver uma nova versão
- Download seguro pelo próprio aplicativo, validação SHA-256, substituição da versão anterior e reinício automático
- Interface adaptável a Full HD, 2K e 4K, respeitando a escala de DPI do Windows
- Temas Light off-white e Dark grafite com azul neon
- Preferências da interface salvas localmente por usuário do Windows, separadas da sessão e dos dados sincronizados pelo Firebase
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
