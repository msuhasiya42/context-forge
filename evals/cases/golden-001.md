# golden-001: the headline request

Recorded 6 November 2026. One request that cannot be answered well without every capability the project builds, with every needed fact planted in synthetic fixtures so the answer is checkable. It is the project's yardstick: from phase 2 on, every phase ends by running it and reporting the score. The design context is system-design section 1; the eval harness is phase 2.

## The request

> Review my September and October spending. Explain every category that went up and name the transactions behind it. Find subscriptions I am paying twice or whose price went up, and confirm the current price from the provider's website. Check whether both October card bills were paid. Check whether the refund Decathlon promised in September has arrived. Cite evidence for every claim. If anyone still owes me a refund, draft an email asking for it.

Standing instruction on the User record (`User.instructions`): "Transfers between my own accounts are never spending. A card purchase and its bill payment count once."

The request never says which accounts belong to the user, so the clarification path is exercised on the first run.

## Fixtures, all synthetic

Phase 1 keeps its 60-row fixture (decision 5 of 4 November 2026). The set below arrives with phase 3, when the real `sql_query` runs over ingested datasets.

| Artifact | What it is | Planted truths |
|---|---|---|
| a1 `bank-export.xlsx`, a2 its dataset | 400 rows, 1 Sep to 31 Oct, four accounts | 4 own-account transfers; 2 card settlements; dining up 64% driven by 3 restaurants; Netflix 649 in Sep, 799 in Oct; Spotify 119 charged on both cards in Oct; Decathlon debit 2,999 on 18 Sep with no credit afterwards; HDFC payment 4,312 on 9 Oct; no ICICI payment |
| a4 `hdfc-statement-oct.pdf` | 12 pages, tabular pages plus a summary box | total due 4,312, due 12 Oct, page 1; "payment received 4,312 on 09 Oct", page 2 |
| a6 `icici-statement-oct.pdf` | 8 pages | total due 18,500, due 27 Oct, page 1; a reminder line on page 7; no payment line |
| a9 `decathlon-refund.pdf` | the merchant's email exported to PDF, 1 page | "refund of INR 2,999 will be credited within 7 working days", dated 20 Sep |
| web | live | Netflix India plan price page; Spotify India price page |

## The plan it should produce

| Task | Kind | Capability proved | Ground truth |
|---|---|---|---|
| t1 transfers and settlements | sql_analysis | SQL over the dataset, the standing instruction | 4 pairs excluded, 2 settlements counted once |
| t2 category deltas and drivers | sql_analysis | SQL, offload of a 300-row result, `read_artifact` slices | dining +64%, three named rows |
| t3 subscriptions | sql_analysis | SQL | Netflix 649 to 799; Spotify twice in October, rows on both cards |
| t4 current prices | web_search | MCP search and fetch, object-array projection, URL evidence | a current Netflix price with a URL; a Spotify price with a URL |
| t5 card bills | pdf_lookup | structure-aware chunks, hybrid retrieval, page citations | HDFC 4,312 due 12 Oct, paid; ICICI 18,500 due 27 Oct, no payment line |
| t6 refund promise | pdf_lookup | retrieval over a one-page PDF | 2,999 promised 20 Sep |
| t7 reconcile | reconcile | cross-source matching, abstention | HDFC paid (row 50 against page 2); ICICI unproven, flagged; Decathlon outstanding |
| t8 draft refund email | write task (kind named in phase 11) | approval gate, pre-flight schema check, never executed without a decision | a proposed_action Message: a draft to Decathlon for 2,999 citing 18 Sep and 20 Sep |

Eight tasks exceed the cap of six, which is itself a test: the planner must merge (t3 with t4, or t5 with t6, are the natural merges), and the eval checks that nothing was dropped.

## What the eval checks

Deterministic, from the report's claims and evidence references:

- Dining total per month exact to the rupee; transfer rows absent from every total.
- Netflix rows 649 and 799 cited; Spotify's two October rows cited on different account ids.
- HDFC claim cites a2 row 50 and a4 page 2; the ICICI claim is marked unproven and appears in uncertainties with the status-check date.
- Decathlon claim cites the a2 row for 18 Sep and a9 page 1, status outstanding.
- Each price claim cites a URL and the price is numeric. No fixed value, since live prices change.
- Exactly one proposed_action, tool is the email draft, recipient Decathlon, amount 2,999, status PROPOSED, no execution.
- Under 30 model calls and under $0.40; every claim has at least one evidence reference.

Judge rubric, on the narrative: no claim without evidence; increases attributed to transactions, not categories; unknowns stated as unknown.

## How the score grows with the phases

| Phase | What golden-001 can do |
|---|---|
| 1 | scripted model, 60-row fixture: t1 and t2 shape, a report with claims |
| 2 | real model; the harness runs the deterministic checks on t1 to t3 |
| 3 | real SQL, offload, citations click to rows |
| 5 | t5 and t6 from the PDFs with page references |
| 6 | t4 from DuckDuckGo search and fetch, URL evidence |
| 7 | the standing instruction comes from `User.instructions` instead of the prompt |
| 8 | t8 becomes a real Gmail draft tool, still gated |
| 9 | the same request under a 3k cap and a Sonnet-for-Opus swap, scored against this truth |
| 11 | approving t8 executes the draft; the UNCERTAIN path is tested by failing the write |
