# Annotation guide — validating extracted interactions

You're checking interactions a tool pulled out of news articles. Each row in
`annotation_sample.csv` is one claimed interaction: a **source** actor doing something **to**
a **target** actor, with the **quote** it came from and a link to the article. Judge each row
**from the quote** (open the article link if you need context). Fill the 6 columns. ~200 rows.

You do **not** need to know any hypothesis — just label what the text says.

## Columns to fill

1. **actors_ok** — `Y` / `N`. Are both actors right? Resolve people/capitals to their state
   (e.g. "Sharif" or "Islamabad" → Pakistan; "Washington"/"Trump" → US; "Tehran" → Iran).
   `N` if either actor is wrong or shouldn't be one of these countries.

2. **direction_ok** — `Y` / `N`. Is **source** the one *doing/saying* the action, and **target**
   the one it's aimed at? `N` if they're reversed.

3. **polarity** — `positive` / `negative` / `neutral`. Judge from THIS sentence's context, not the
   dictionary meaning of the word:
   - **positive** = cooperative/friendly: talks, meetings, support, praise, aid, agreement,
     ceasefire, mediation, hosting, a mediator urging restraint.
   - **negative** = hostile/adversarial: attacks, strikes, threats, accusations, condemnation,
     sanctions, rejections, ultimatums, demands to surrender.
   - **neutral** = contact with no clear cooperative or hostile stance.
   Examples: *"Pakistan urged both sides to extend the ceasefire"* → positive.
   *"Israel demanded Iran surrender or face attack"* → negative.
   *"officials from the two sides met briefly"* → neutral.

4. **real_event** — `Y` / `N`. Did this actually happen as stated? `N` if it's hypothetical, denied/
   negated, only a future possibility, or pure commentary by a journalist/analyst rather than an
   act by the state actor.

5. **mediation** — *fill ONLY if you marked polarity = positive; otherwise leave blank.*
   - `mediation` = genuine go-between/brokering work: hosting talks, mediating, facilitating,
     relaying messages, convening, urging the rivals to negotiate/de-escalate.
   - `goodwill` = friendly but not brokering: praise, thanks, general support, a basing/trade deal.
   - `not` = positive but unrelated to mediating a conflict.

6. **notes** — optional. Anything unclear, or why you marked something `N`.

## How to decide quickly
- Read the quote. If it doesn't actually support the claimed source→target interaction, mark the
  relevant box `N` and move on.
- When the quote is ambiguous, open the article link before deciding.
- Don't overthink polarity — use the three buckets above; if truly unsure, `neutral`.

## Logistics
- Edit `annotation_sample.csv` directly (Excel / Google Sheets / any CSV editor) and send it back.
- Allowed values: actors_ok/direction_ok/real_event = `Y` or `N`; polarity = `positive`/`negative`/
  `neutral`; mediation = `mediation`/`goodwill`/`not` (or blank).
- If two people are annotating, both do the **same first 50 rows** (E001–E050) so we can measure
  agreement, then split the rest.

Thank you — this is what lets us report human-validated accuracy instead of "the tool said so."
