# Verification scenarios

Use these scenarios to review changes to the skill. Numbers below are synthetic
fixtures, never vendor prices. An acceptable response explains the evidence gap
and produces the appropriate JSON status without inventing missing information.

| Scenario | Expected behavior |
| --- | --- |
| A Vertex model is paired with a Gemini Developer API table | Reject the product mismatch; open the Vertex source or report unverified. |
| The official rate is USD 2 per million tokens; configuration treats it as per thousand | For 1,000 tokens calculate USD 0.002, identify the 1,000-fold discrepancy, report mismatch. |
| 1,000 input tokens include 200 cached tokens | Apply ordinary input pricing to 800 and cache-read pricing to 200; do not bill 1,000 ordinary plus 200 cached. Require the vendor's inclusion semantics. |
| Parent Responses usage and image-tool usage both appear | Establish their overlap and tool charges from official documentation before adding them; unresolved overlap cannot become a verified total. |
| Video uses 1080p, audio and a source video; only 720p text-to-video pricing is known | Require the exact resolution/audio/source-video rules and actual usage; do not reuse the nearby profile. |
| ElevenLabs consumer credits are available but API tier/metering is missing | Do not amortize a subscription or infer API dollars per credit. Open ElevenAPI pricing and applicable account evidence or report unverified. |
| An official page requires rendering or inaccessible account access | Use authorized available access. If evidence still cannot be opened, cite the limitation and leave rates unverified. A search snippet is insufficient. |
| The requested exact model/snapshot is absent from official pricing | Report unverified; do not substitute a Lite/Pro/successor model. |
| A provider supplies an inclusive monetary charge plus token/tool usage | Verify currency/units and use the reported charge once. Preserve a separate calculated comparison; never add it to the inclusive charge. |
| Today's price is available but the task concerns last month's invoice | Require the historical effective interval. Retrieval today alone cannot verify last month's price. |

Repository automated tests cover evidence-source boundaries and calculation
invariants. These scenarios also require a review of the agent's readable report
and evidence-selection behavior; passing schema validation alone cannot establish
that the source was opened or that its conditions apply.
