"""Owner's Office pipeline.

Hard rule: model calls may live only in pipeline/llm.py (CI check C-LLM-ENTRY).
When another module in this package needs a model, it calls pipeline.llm.complete() instead of importing the model
SDK or running the Claude Code CLI itself.

- llm.py: the only model-call entry point. Assembles requests from agents/*.yml and the front matter of the prompts,
  trims inputs, validates outputs and retries once when they fail, and logs every request. Backends
  (decisions/0022): claude-code, the Claude Code CLI on the owner's Claude subscription (the default); api, the
  Anthropic API (the fallback, and the only spend that counts against the monthly budget); fake, for dry runs.
- outputs.py: parsing and validation of <output> envelopes, generated_by, placement per 00 §F2 (calls no model).
- isolation.py: removes from finished products the sections that audit-type inputs must not see (00 §G6; calls no
  model).
- registry.py: the runner's input registry: the supported steps and how each declared input is assembled from the
  two repositories, EDGAR and earlier runs (decisions/0019; calls no model).
- runner.py: the pipeline runner, one prompt part at a time: assemble an input bundle, execute its model call through
  llm.complete(), place the outputs; dry runs with the fake backend (decisions/0019).
- fake_client.py: the fake model client for dry runs: schema-valid placeholders, no network (calls no model).
- edgar.py: SEC EDGAR access (the User-Agent is read only from the environment or the workspace .env), earnings events
  and release_history, estimates of the next release date and the pre-registration deadline, checking accession
  numbers in sources.yml (decisions/0017; calls no model).
- timestamp.py: OpenTimestamps timestamps for pre-registration files: stamp, upgrade, verify (decisions/0018; calls
  no model).
"""
