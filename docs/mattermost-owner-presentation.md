# Contrato owner-facing para Mattermost

## Objetivo

Este documento define como resumir o trabalho para o owner em linguagem simples, com hierarquia clara e sem ruído técnico desnecessário.

## Exemplos aprovados

Use mensagens curtas, com abertura direta e o status logo no início.

- `decision`: pedir uma decisão explícita.
- `status`: mostrar o andamento atual.
- `progress`: indicar progresso sem concluir.
- `final`: encerrar com resultado validado.
- `failure`: deixar claro que a entrega não concluiu.
- `duplicate`: sinalizar repetição e bloqueio.
- `cron`: indicar que o próximo passo depende de agendamento.
- `watchdog`: indicar espera por sinal externo.
- `self_review`: registrar revisão própria antes do fechamento.

## Limites

- Cada mensagem deve respeitar o limite de linhas do caso.
- O texto precisa começar com um resumo em linguagem simples.
- Não use títulos Markdown dentro da mensagem da fixture.
- Preserve a hierarquia: primeiro o status, depois o contexto, depois a ação ou o próximo passo.

## Termos proibidos

Evite copiar IDs, nomes pessoais, tokens, URLs sensíveis, payloads técnicos e detalhes que identifiquem threads reais.

Não use linguagem interna que só faça sentido para quem implementou a automação.

## Exceções

Quando a mensagem for terminal, ela pode encerrar com uma frase curta de validação ou com uma nota de falha clara.

Quando a mensagem não for terminal, ela deve deixar explícito o próximo passo ou o marco seguinte.

## Revisão visual

Antes de liberar o contrato, revise visualmente que:

- a primeira linha é fácil de ler;
- não há excesso de linhas;
- a mensagem não parece um log;
- o owner entende a ação sem contexto técnico;
- o tom permanece direto e respeitoso.
