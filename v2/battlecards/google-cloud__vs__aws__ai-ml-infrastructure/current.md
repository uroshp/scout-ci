# Competitive Intelligence Brief: Google Cloud vs AWS

## Executive Summary

**Inference economics decide the AI bill, and our owned silicon wins there.**

TPU 8i serves close to twice the volume at the same cost, Blackstone just put \$5B behind TPU capacity over Nvidia, and AWS rents its frontier models on largely sold-out Trainium.

**Soundbite:** "Inference is your real AI bill. Price your token volume on Gemini Flash and see the gap."

**So what:** Move the deal from who is bigger to who is cheaper per token at scale. ([blog.google](https://blog.google/innovation-and-ai/infrastructure-and-cloud/))

**AWS has no first-party frontier model: it rents its AI soul from Anthropic and OpenAI. Google owns the whole stack.**

AWS's model story now rests on partners it doesn't control: it expanded its Anthropic position to up to \$25B, and Anthropic committed to spend more than \$100B on AWS over ten years. Bedrock added OpenAI's GPT-5.5 in June. Google, by contrast, owns silicon (TPU), the frontier model (Gemini/DeepMind), the platform, and the distribution channel under one roof.

**So what:** When procurement asks "who controls the model roadmap?", the AWS answer is Anthropic and OpenAI, companies with their own agendas. On Google Cloud it's Google. Make roadmap control a buying criterion. ([cnbc.com](https://www.cnbc.com/2026/04/20/amazon-invest-up-to-25-billion-in-anthropic-part-of-ai-infrastructure.html))

**On inference economics, where the real AI spend now lives, Google's custom silicon is the cost wedge.**

Google's 8th-gen TPU 8i (announced April 22, 2026) is purpose-built for inference and, per Google, delivers 80% better performance-per-dollar than the prior generation, letting customers serve nearly twice the volume at the same cost. AWS's competing Trainium3 is capable but is largely sold out and ramps on a conventional fabric.

**So what:** Ask the prospect what share of their AI bill is inference. Above ~60%, the TPU cost-per-token story is your strongest close: build the TCO model on the spot. ([blog.google](https://blog.google/innovation-and-ai/infrastructure-and-cloud/google-cloud/eighth-generation-tpu-agentic-era/))

**Be honest about where we're exposed: reliability and capacity are live objections the rep will be asked.**

Google Cloud's June 2025 global outage (70+ services) and Pichai's own "compute constrained" admission are both real and will surface in deals. AWS has its own outages, but our reps must own ours, not duck them.

**So what:** Don't lead with reliability. Lead with AI economics and roadmap control, and have a rehearsed, evidence-based answer ready for the outage and capacity objections (see Objection Handling). A prepared answer beats a defensive one. ([cnbc.com](https://www.cnbc.com/2025/06/16/google-cloud-outage-apology.html))

## Snapshot

- AWS posted Q1 2026 net sales of \$37.6B (up 28% YoY) with operating income of \$14.16B, an audited beat that materially topped the ~\$12.84B consensus. AWS remains Amazon's primary profit engine. ([cnbc.com](https://www.cnbc.com/2026/04/29/aws-earnings-q1-2026.html))
- Google Cloud posted Q1 2026 revenue of \$20.03B, up 63% YoY, beating the \$18.05B estimate, its first quarter above \$20B and more than double AWS's growth rate. ([cnbc.com](https://www.cnbc.com/2026/04/29/alphabet-googl-q1-2026-earnings.html))
- Worldwide cloud-infrastructure market share in Q1 2026 (Synergy Research ESTIMATE): AWS 28%, Microsoft Azure 21%, Google Cloud 14%. AWS still leads, but Azure and Google are growing materially faster, narrowing the gap. ([srgresearch.com](https://www.srgresearch.com/articles/cloud-market-annual-revenue-run-rate-topped-half-a-trillion-dollars-in-q1-as-growth-surge-continues))
- Matt Garman is the current CEO of AWS, in the role through the AWS re:Invent 2025 cycle and Q1 2026 reporting. ([cnbc.com](https://www.cnbc.com/2026/04/29/aws-earnings-q1-2026.html))
- Amazon guided to roughly \$200B in 2026 capital expenditures, the bulk for AI infrastructure (data centers, Trainium, Nvidia GPUs), with Q1 2026 capex alone at \$44.2B. This is a company-stated guidance figure. ([cnbc.com](https://www.cnbc.com/2026/04/20/amazon-invest-up-to-25-billion-in-anthropic-part-of-ai-infrastructure.html))

## Recent Strategic Moves

- On June 25, 2026, Amazon committed an additional \$13B to expand AWS AI and cloud capacity in India by 2030, taking its planned India AI/cloud investment to more than \$21B for 2026-2030 and total India commitments to \$48B over the next five years. The new money expands AWS data-center capacity in Mumbai and Hyderabad with Trainium chips and Bedrock. For context, Google pledged \$15B in October 2025 for its first Indian AI hub in Andhra Pradesh, and Microsoft pledged \$17.5B in December 2025. ([mobileworldlive.com](https://www.mobileworldlive.com/ai-cloud/amazon-pumps-additional-13b-into-india-ai-cloud/))
- AWS brought Graviton5 (its first custom CPU purpose-built for agentic AI) to general availability on June 10, 2026 in the EC2 M9g/M9gd instances, extending AWS's silicon story from training (Trainium) into CPU-bound agentic inference (real-time reasoning, code generation, multi-step orchestration). The 3nm, 192-core chip delivers up to 25% better compute performance than Graviton4, and AWS landed marquee validation: Meta committed to deploy 'tens of millions' of Graviton5 cores under a multibillion-dollar deal, with Uber and Snowflake also onboard, joining 120,000+ existing Graviton customers. ([aboutamazon.com](https://www.aboutamazon.com/news/aws/meta-aws-graviton-ai-partnership))
- On June 1, 2026, AWS made OpenAI's GPT-5.5, GPT-5.4 and Codex generally available on Amazon Bedrock at OpenAI-matching token rates, its first frontier OpenAI access, landing weeks after Microsoft's exclusivity lapsed. So what for us: it confirms Bedrock is a model marketplace, not a differentiated first-party model. Google still has the only fully-owned frontier model (Gemini) trained on its own silicon. ([aboutamazon.com](https://www.aboutamazon.com/news/aws/bedrock-openai-models))
- On April 20, 2026, Amazon agreed to invest up to \$25B more in Anthropic (on top of ~\$8B prior), and Anthropic committed to spend \$100B+ on AWS over ten years, including up to 5GW of Trainium capacity. So what for us: AWS is deepening a dependency on a partner it doesn't own, and Claude still runs on Google Cloud too, so "we need Claude" is not a reason to pick AWS. ([cnbc.com](https://www.cnbc.com/2026/04/20/amazon-invest-up-to-25-billion-in-anthropic-part-of-ai-infrastructure.html))
- In his April 2026 shareholder letter, Andy Jassy disclosed that Trainium2 (roughly 30% better price-performance than comparable GPUs) is "largely sold out" and Trainium3 is "nearly fully-subscribed." So what for us: even buyers who want Trainium can't get it: sell Google Cloud TPU and GPU availability today, not spec-sheet comparisons. ([cio.com](https://www.cio.com/article/4157494/ai-demand-is-so-high-aws-customers-are-trying-to-buy-out-its-entire-capacity-2.html))
- A 13-hour AWS outage in December 2025 was reportedly caused by Amazon's own Kiro agentic AI tool autonomously deciding to delete and recreate an environment (Financial Times, via Engadget). Amazon disputes the framing, calling it "an extremely limited event" affecting only AWS Cost Explorer in one region. So what for us: it's a governance talking point for CISOs weighing AWS for mission-critical AI, but cite it honestly, including Amazon's denial. ([engadget.com](https://www.engadget.com/ai/13-hour-aws-outage-reportedly-caused-by-amazons-own-ai-tools-170930190.html))
- In January 2026 AWS quietly raised EC2 Capacity Block prices for its H200 GPU instances ~15% (the p5e.48xlarge jumped from \$34.61 to \$39.80/hr), reversing its usual "prices only go down" posture amid GPU scarcity. So what for us: this breaks the AWS cost-leadership narrative for guaranteed GPU capacity and hands Google Cloud a pricing talking point. ([theregister.com](https://www.theregister.com/2026/01/05/aws_price_increase/))

## Positioning and Differentiation

- AWS positions Bedrock as the broadest frontier-model marketplace, now spanning OpenAI (GPT-5.5/5.4, Codex), Anthropic Claude, Meta Llama, Mistral and its own Amazon Nova through one API. This is AWS's own positioning; the strategic tell is that AWS aggregates other labs' models rather than fielding a first-party frontier model of its own. ([aboutamazon.com](https://www.aboutamazon.com/news/aws/bedrock-openai-models))
- AWS positions Trainium3 (launched re:Invent 2025) as a cost play, claiming up to 50% savings vs GPU training. This is AWS's own pre-GA benchmark, not independently audited, and the chip's ecosystem still depends on the AWS Neuron SDK, which is narrower than CUDA. ([datacenterknowledge.com](https://www.datacenterknowledge.com/data-center-chips/aws-launches-tranium3-chip-to-challenge-nvidia-ai-dominance))
- AWS claims the broadest Nvidia GPU lineup of any cloud, pledging to add more than 1 million Blackwell and Rubin GPUs across regions starting in 2026 (its own claim). It's a real strength, but it also underlines how dependent AWS's high-end AI compute is on Nvidia supply. ([aws.amazon.com](https://aws.amazon.com/blogs/machine-learning/aws-and-nvidia-deepen-strategic-collaboration-to-accelerate-ai-from-pilot-to-production/))
- Google Cloud's differentiator is fabric-scale custom silicon: with TPU 8t and its Jupiter/Virgo networking, Google says it can knit 1M+ TPU chips into a single training cluster: the same infrastructure that trains Gemini, offered to customers. AWS Trainium scales across multiple clusters, not one fabric of comparable reach. ([cloud.google.com](https://cloud.google.com/blog/products/compute/ai-infrastructure-at-next26))

## Pricing and Packaging

- Unlike AWS, Google Cloud applies automatic sustained-use discounts to eligible attached GPUs as monthly usage rises (no upfront commitment required), on top of optional committed-use discounts. This is a structural flexibility advantage for variable AI workloads. ([cloud.google.com](https://cloud.google.com/compute/gpus-pricing))
- Google is pressing a token-cost advantage: it says enterprises running ~1 trillion tokens/day on Google Cloud could save \$1B+ annually by shifting ~80% of workloads to Gemini 3.5 Flash (and a mix of frontier models), economics enabled by TPU inference. AWS has no first-party model offering comparable price/performance leverage. So what: token-budget exhaustion is a live enterprise pain point to quantify in a TCO model. ([venturebeat.com](https://venturebeat.com/technology/google-says-gemini-3-5-flash-can-slash-enterprise-ai-costs-by-more-than-1-billion-a-year))

## Competitive Battlecard

### Where Google Cloud wins

**We win the largest-scale training and inference workloads on silicon depth.**

For AI-native startups, frontier labs and research-heavy enterprises, Google's 8th-gen TPUs (8t/8i) and single-fabric scale to 1M+ chips are a genuine moat: customers run on the same hardware that trains Gemini. AWS Trainium is competitive on cost-per-token but supply-locked and scales across multiple clusters, not one fabric.

**Soundbite:** *"You can run on the exact infrastructure that trains Gemini (one fabric, a million-plus chips) and you can actually get the capacity, today."* ([cloud.google.com](https://cloud.google.com/blog/products/compute/ai-infrastructure-at-next26))

**External capital is validating TPU demand, not just Google's own books.**

On May 19, 2026, Blackstone committed \$5B to a Google-backed venture to sell TPU capacity as a service, targeting 500MW by 2027. A third party betting billions on TPU (not Nvidia) compute is independent proof that TPU price-performance is credible at enterprise scale. AWS has no comparable outside vote of confidence in Trainium.

**Soundbite:** *"When Blackstone puts \$5 billion behind TPU capacity, that's the market, not Google's marketing, saying this silicon is the real thing."* ([cnbc.com](https://www.cnbc.com/2026/05/19/blackstone-google-ai-data-center-joint-venture-tpu.html))

**We just closed our biggest historical gap: enterprise delivery muscle.**

On June 4, 2026, IBM and Google Cloud launched a joint practice putting thousands of IBM consultants behind industry-specific AI agents (banking, healthcare, telecom, government) on Gemini Enterprise. For regulated buyers who once rejected Google for thin systems-integrator support, there's now a credentialed delivery partner.

**Soundbite:** *"Worried about delivery depth? You now get IBM's consultants building your agents on Google's stack. That's a combination AWS can't simply match."* ([newsroom.ibm.com](https://newsroom.ibm.com/2026-06-04-ibm-and-google-cloud-announce-strategic-partnership-to-scale-ai-with-human-expertise-and-ai-powered-delivery))

### Where it's a fight

**Model breadth is now a real fight, not a Google win.**

Both platforms offer 200+ models. Bedrock's June addition of OpenAI GPT-5.5 alongside Claude, Llama and Nova is a genuine strength, and its token spend is growing fast. Google counters with first-party Gemini (long-context, multimodal) plus partner models on the Gemini Enterprise Agent Platform. The tie-breaker is rarely model parity. It's data gravity, integration and price.

**Soundbite:** *"If you just want a model menu, both clouds have one. The question is which one also owns the model and the chip underneath it. That's us."* ([aboutamazon.com](https://www.aboutamazon.com/news/aws/bedrock-openai-models))

**Raw Nvidia GPU availability is a shared constraint, not a clean differentiator.**

Both clouds are supply-limited on top-end Nvidia capacity (TSMC CoWoS and HBM bottlenecks), and both are co-engineering with Nvidia on next-gen Rubin systems. Whoever can offer a credible custom-silicon alternative reduces the customer's GPU-queue risk, and that favors whichever side the buyer trusts on their specific framework (CUDA vs TPU/JAX/PyTorch).

**Soundbite:** *"Nobody has unlimited Blackwell right now. The difference is we give you a real second path, TPUs, so you're not stuck in one GPU line."* ([aws.amazon.com](https://aws.amazon.com/blogs/machine-learning/aws-and-nvidia-deepen-strategic-collaboration-to-accelerate-ai-from-pilot-to-production/))

Agent-platform maturity is now a contested fight, not a clean Google win. At AWS Summit New York (June 17, 2026), AWS shipped a production-agent stack on Bedrock AgentCore: the AgentCore harness reached general availability, alongside a managed Knowledge Base for enterprise RAG, native Web Search grounding inside the customer's AWS environment, and agent observability with recommendations and A/B testing. AWS also added AWS WAF AI traffic monetization (letting publishers charge AI bots for content) and previewed two agent-infrastructure services, AWS Context (a company-wide knowledge graph for agents) and AWS Continuum (agentic code-vulnerability security). AWS says tasks run on AgentCore grew 15x in six months. This narrows the production-agent gap reps used to claim over Vertex AI and the Gemini Enterprise Agent platform. ([aws.amazon.com](https://aws.amazon.com/blogs/aws/top-announcements-of-the-aws-summit-in-new-york-2026/))

### Where AWS wins

**AWS wins on incumbency and data gravity. Be honest about it.**

With ~28% market share and revenue nearly double ours, AWS holds the install base, and Jassy's pitch is blunt: customers want inference next to data that already lives in AWS. For a CIO with a decade of AWS footprint, switching cost is the dominant force in the deal.

**Soundbite:** *"We're not asking you to rip out AWS. We're asking you to put your highest-value AI workloads where the silicon and economics are best, and connect back to your AWS data."* ([aboutamazon.com](https://www.aboutamazon.com/news/company-news/amazon-ceo-andy-jassy-aws-ai-q1-2026-earnings))

**If a buyer has standardized on Claude, AWS has the deepest native path.**

Over 100,000 customers run Claude on Bedrock, Trainium handles the majority of Bedrock inference traffic, and the AWS–Anthropic co-engineering (Project Rainier, \$100B commitment) is hard to match at arm's length. Claude is on Vertex too, but the integration depth on Bedrock is real.

**Soundbite:** *"Claude runs great on Bedrock, and it runs on Vertex too, without Bedrock's quota waits. Same model, your choice of infrastructure."* ([techcrunch.com](https://techcrunch.com/2026/03/22/an-exclusive-tour-of-amazons-trainium-lab-the-chip-thats-won-over-anthropic-openai-even-apple/))

**AWS's service breadth and partner ecosystem are a default-platform advantage.**

Thousands of services, the largest ISV and SI ecosystem, and the broadest compliance coverage make AWS the safe anchor for multi-cloud enterprises. Our \$20B revenue and ~14% share mean proportionally fewer integrations and certifications, a gap the rep should acknowledge and route around.

**Soundbite:** *"AWS has more services. For AI/ML specifically (silicon, model, economics), we have the edge, and you can keep the rest of your AWS estate."* ([aboutamazon.com](https://www.aboutamazon.com/news/company-news/amazon-ceo-andy-jassy-aws-ai-q1-2026-earnings))

## Sentiment

- Sentiment: enterprise reviewers report unpredictable Bedrock billing: one PeerSpot reviewer cited ~\$130 in unexpected charges within two weeks without even deploying a model. Recurring theme: cost opacity as teams move past proof-of-concept. ([peerspot.com](https://www.peerspot.com/products/amazon-bedrock-reviews))
- Sentiment: SageMaker draws consistent complaints about month-end billing shock, a steep learning curve for non-AWS-native teams, and "walled garden" lock-in that penalizes multi-cloud strategies (G2/PeerSpot themes). ([truefoundry.com](https://www.truefoundry.com/blog/amazon-sagemaker-review-features-pricing-pros-and-cons-better-alternative))
- Sentiment: developers describe Trainium's Neuron SDK as painful outside AWS's happy path ("things tend to fall apart immediately" with custom dependencies) and note the conspicuous absence of public customer endorsements beyond Anthropic. A real adoption-friction signal for the Trainium ecosystem. ([news.ycombinator.com](https://news.ycombinator.com/item?id=46125155))
- Sentiment (the other side): enterprises praise Bedrock for low operational lift and tight IAM/VPC integration within existing AWS security boundaries, a genuine strength for compliance-heavy shops already standardized on AWS. Don't pretend AWS has no fans. ([peerspot.com](https://www.peerspot.com/products/amazon-bedrock-reviews))
- Sentiment (our own weak spot): Vertex AI / Gemini Enterprise Agent Platform draws complaints about complex multi-dimensional pricing and a steep learning curve for teams not already GCP-native. Reps should expect this and have an onboarding/cost-modeling answer ready. ([tekpon.com](https://tekpon.com/software/google-cloud-vertex-ai/reviews/))

## Objection Handling

**"Google Cloud had that massive 2025 outage. Why trust critical AI to them?"**

Own it directly: the June 12, 2025 outage took down 70+ services for hours after an untested global config change, and Google publicly apologized and committed to feature-flagged rollouts and isolation so one fault can't cascade. Every hyperscaler has had a major outage (AWS's us-east-1 included); the question is the fix trajectory, and the market clearly hasn't defected, given 63% Q1 2026 growth.

**So what:** Don't minimize it: acknowledge, point to the concrete process changes, and pivot the reliability conversation to forward architecture, not past incidents. ([cnbc.com](https://www.cnbc.com/2025/06/16/google-cloud-outage-apology.html))

**"Google kills products. You just retired Vertex AI. Why bet our ML platform on that?"**

This is grounded and fair to raise. But the April 22, 2026 change is an evolution, not a shutdown: Google explicitly described the Gemini Enterprise Agent Platform as "the evolution of Vertex AI," carrying forward model selection, model building and agent building, with existing workloads preserved. The honest counter is to negotiate continuity into the contract (SLA uplift, migration support, named-service commitments) and point to anchor customers who ran this same analysis and stayed.

**So what:** Convert the deprecation fear into contractual commitments rather than denying the pattern; that's what earns credibility. ([cloud.google.com](https://cloud.google.com/blog/products/ai-machine-learning/introducing-gemini-enterprise-agent-platform))

**"Even Pichai says Google Cloud is compute-constrained. We'll be stuck in a queue."**

True and CEO-stated: "We are compute constrained in the near term." But it's a demand problem, not a viability one: the backlog and a \$180–190B 2026 capex plan, plus the Blackstone TPU venture, are closing the gap, and AWS admits the same constraint (Trainium sold out). The right move is to secure committed capacity through the account team now, not to assume an equally-constrained AWS is more available.

**So what:** Reframe constraint as proof of demand, and get the customer a committed-capacity agreement rather than letting them walk to AWS over a shared industry condition. ([cnbc.com](https://www.cnbc.com/2026/04/29/alphabet-googl-q1-2026-earnings.html))

**"We want Claude, and that means AWS Bedrock."**

Not exclusive: Anthropic itself states Claude is available on all three major clouds, including Google Cloud. Running Claude on Vertex pairs the model with Google's TPU infrastructure and avoids the Bedrock quota waits some customers have hit: same frontier model, your choice of infrastructure and economics.

**So what:** Neutralize the "Claude = AWS" reflex early; make the conversation about which infrastructure runs Claude best, not which cloud has it. ([anthropic.com](https://www.anthropic.com/news/anthropic-amazon-compute))

**"AWS is the market leader. That's the safe pick."**

Fair on today's numbers: AWS holds ~28% share to Google's 14% (Synergy). But "safe" is a snapshot, and the snapshot is moving: Google Cloud grew 63% YoY vs AWS's 28%, led by enterprise AI. The safer long-term bet is the platform whose AI economics and model ownership are pulling the market toward it.

**So what:** Concede the current-share point, then redirect to a 3–5 year trajectory framing where the growth gap and AI differentiation favor Google. ([srgresearch.com](https://www.srgresearch.com/articles/cloud-market-annual-revenue-run-rate-topped-half-a-trillion-dollars-in-q1-as-growth-surge-continues))

**"Google Cloud has an active, unresolved network incident in India. That's not a historical footnote. It's happening right now."**

Own the scope completely: a fire at a third-party Delhi facility on June 9 forced an emergency power shutdown that isolated a local Point of Presence. As of the June 23 status update (the most recent available), the incident remains active and unresolved 15 days on. Traffic from Delhi, Chennai, and Mumbai continues to hit intermittent elevated latency and possible packet loss, with no workaround available. Affected services span Hybrid Connectivity, Media CDN, and VPC in asia-south2, with some global impact on those same services. Google's team has now reached the damaged site and is restoring capacity through the week; the next status update is not due until Monday, June 29.

**So what:** Map the buyer's actual workloads against that scope right now in the conversation. Ask which services and regions they are evaluating. If their target deployment does not touch Hybrid Connectivity, Media CDN, or VPC in the affected India regions, this incident has no operational impact on them today. Confirm that explicitly, then redirect to the workload they came in to evaluate. ([status.cloud.google.com](https://status.cloud.google.com/incidents/5fGQt4VbkDnr3Yp8PXPr))

## Cut Log

This is what verification removed or corrected during fact-checking, and why.
- **CUT — Epic Games moved a \$10M Fortnite AI project to Google Cloud after AWS Bedrock capacity failures.:** Traces to a single paywalled Business Insider report; the dnyuz syndication mirror returned 403 and no Tier-1/2 outlet independently corroborated it. Not reliably groundable, and the only fetchable echo was a non-news blog (Last Week in AWS), which fails news-first sourcing for an adverse competitive claim.
- **CUT — May 7-8 2026 AWS us-east-1 thermal event took down 150+ cloud services.:** Only sourced to StatusGator, a status-page aggregator blog (Tier 3/4); the '150+' figure is its own methodology and no reputable Tier-1/2 news outlet covered the outage. A current-state/status claim must anchor on Tier-2 news or a primary filing, so it was removed. The Kiro-caused December outage (Engadget/FT, Tier 2) is retained as the AWS reliability signal instead.
- **CUT — AWS planning to add Elon Musk's Grok to Bedrock despite 'zero enterprise demand' ('revenge porn edgelord LLM').:** Single-source rumor with inflammatory framing; inconsistent with the brief's professional, non-combative tone standard and not load-bearing for the AI-infra thesis.
- **CUT — Amazon cut ~30,000+ corporate jobs including AWS across three 2026 waves.:** Best available anchors were trade-aggregator/blog sources (KORE1, Last Week in AWS) and the AWS-specific concentration could not be confirmed via a Tier-1/2 outlet. Omitted to keep sourcing discipline rather than anchor an adverse claim on weak sources.
- **REVISED — TPU 8i delivers 80% better performance-per-dollar for inference.:** Proposed evidence excerpt was a paraphrase; replaced with the verbatim span from blog.google ('These innovations deliver 80% better performance-per-dollar compared to the previous generation...') so it grounds character-for-character. Number unchanged; labeled as Google's own (Tier 1B) claim.
- **REVISED — AWS 13-hour December 2025 outage caused by its own Kiro AI tool.:** Added Amazon's on-record denial (an 'extremely limited event' affecting only Cost Explorer in one region) for balance, since the adverse framing comes from FT reporting that Amazon disputes.
- **REVISED — Cloud market share figures.:** Standardized to a single reputable source (Synergy Research: AWS 28% / Azure 21% / Google 14%) and dropped conflicting 30-31%/12-13% figures from secondary aggregators to keep one value per metric.
- **REVISED — 'Google kills products' objection sourcing.:** Re-anchored from killedbygoogle.com and a dev.to post onto the official Google Cloud blog announcing the Gemini Enterprise Agent Platform as 'the evolution of Vertex AI,' which is both more reputable and directly on-point for the objection's honest rebuttal.
- **CUT — Objection-handling block on Google account suspensions (Railway, May 2026; the 2024 UniSuper wipe):** the cited source was unreachable during verification, so the block was removed rather than left ungrounded.