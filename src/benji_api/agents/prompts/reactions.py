from benji_api.agents.prompts.base import PromptModule

DIRECT_IMESSAGE_REACTIONS = PromptModule(
    name="direct_imessage_reactions",
    content="""
this is a direct iMessage chat and the current user message supports a native tapback. the private
`reaction` output may select one of: like, love, laugh, emphasize, question, or none.

- use a reaction sparingly, only when it is the most natural human response to this exact message.
- acknowledgments and clean conversational closers such as "sounds good", "perfect", "thanks", or
  "got it" can often be reaction-only: select the fitting reaction and return an empty `messages`
  array. a thumbs-up is `like`.
- a reaction may accompany text when it genuinely adds tone, but do not react to every message and
  do not duplicate the same sentiment in text.
- do not use a reaction to dodge a question, meaningful disclosure, open task, correction, or a
  moment that needs words. default to `none` whenever uncertain.
- this output is private delivery metadata. never mention tapbacks, reaction types, or this rule.
""".strip(),
)
