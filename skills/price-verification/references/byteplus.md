# BytePlus

Sources: [ModelArk pricing](https://docs.byteplus.com/en/docs/ModelArk/1544106),
[pricing entry](https://docs.byteplus.com/en/docs/ModelArk/1099320),
[image API](https://docs.byteplus.com/en/docs/ModelArk/1541523), exact model/API
documentation reached from these pages, and authorized account billing records.

1. Resolve exact versioned model ID, API base, region, ModelArk versus LAS, and
   standard/fast variant. A LAS profile cannot borrow a ModelArk tariff.
2. Open the pricing page in an authorized browser if the text view contains only
   navigation. Read the table and footnotes, not a search engine's cached extract.
3. Verify currency, token/image/second denominator, resolution, source-video versus
   no-video profile, minimum billable usage and any dated promotional price.
4. For Seedance retain provider `completion_tokens` and profile selectors. Confirm
   whether reported tokens already include source video and minimum consumption;
   do not add estimated source tokens again. A field named `cost` is not USD
   evidence unless the schema explicitly defines its currency and units.
5. For Seedream record actual generated images and actually charged reference
   images/search invocations. Configuring a search tool does not prove it ran.
   Missing outputs must not be replaced with requested `n`. Verify whether an
   included first reference is free before subtracting it.
6. Reproduce the Decimal calculation and compare exact usage/profile with gateway,
   LiteLLM and authorized account billing evidence. Report unresolved model IDs,
   unreadable tables, incompatible profiles and unexplained totals explicitly.
