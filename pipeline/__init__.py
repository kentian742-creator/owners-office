"""Owner's Office pipeline.

Hard rule: model calls may live only in pipeline/llm.py (CI check C-LLM-ENTRY).
When another module in this package needs a model, it calls pipeline.llm.complete() instead of importing the model
SDK itself.

- llm.py: the only model-call entry point. Assembles requests from agents/*.yml and the front matter of prompt set v3,
  trims inputs, guards the budget, writes the log, validates outputs and retries once when they fail.
- outputs.py: parsing and validation of <output> envelopes, generated_by, placement per 00 §F2 (calls no model).
- isolation.py: removes from finished products the sections that audit-type inputs must not see (00 §G6; calls no
  model).
- edgar.py: SEC EDGAR access (the User-Agent is read only from the environment or the workspace .env), earnings events
  and release_history, estimates of the next release date and the pre-registration deadline, checking accession
  numbers in sources.yml (decisions/0017; calls no model).
- timestamp.py: OpenTimestamps timestamps for pre-registration files: stamp, upgrade, verify (decisions/0018; calls
  no model).
"""
