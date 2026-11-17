# Run Colombia Tourism and Email in LangSmith Studio

This development Agent Server publishes two graph IDs, `tourism` and `email`.
The graphs reuse the verified Cloud Run service code. Neither graph sends email,
creates calendar events, or connects to the deployed Colombia services.

| Graph | Synthetic Studio input (Graph mode) |
| --- | --- |
| `tourism` | `{"city":"Bogotá","days":2,"interests":["culture"]}` |
| `email` | `{"message":"Redacta un recordatorio de reunión."}` |

## Build and connect

1. With the LangGraph CLI installed in an isolated development environment, run
   `langgraph dev --config deployments/langsmith/langgraph.json` and verify the
   two graph IDs and the synthetic inputs above.
2. In the intended LangSmith workspace, create a **development** Agent Server
   from the reviewed GitHub branch. Set **LangGraph API configuration file** to
   `deployments/langsmith/langgraph.json`. Leave automatic updates off for the
   initial snapshot and keep the installation limited to this repository.
3. Open Studio in Graph mode and run each sample as a fresh run. Confirm that
   the result includes the model profile and that Email reports `not_attempted`.

## Model and effects

- Default `fixture` uses a deterministic model and the local Tourism catalog.
  It tests workflow behavior, not Gemini output quality. For Tourism, use the
  structured fields; natural language `message` requires Vertex.
- Optional `COLOMBIA_LANGSMITH_MODEL=vertex` uses the original Vertex model
  adapter. It requires separately authorized ADC in the hosting environment
  and can incur model costs. Do not use or export a developer's gcloud
  session or any Cloud Run Gmail, Calendar, signing, or service API credentials.
- The runtime always injects a transport that refuses delivery. No provider
  binding, A2A peer, external catalog, or production workflow store is loaded,
  including in Vertex mode. Tourism's per-run scratch state is discarded.
- Use synthetic inputs only: Studio and LangSmith can retain inputs, outputs,
  and intermediate traces. The Email result does not return approval tokens,
  signed previews, recipient fields, or opaque context.

`deployed-source.json` records the source archive, image, and runtime hashes.
The checked-in runtime files preserve the deployed bytes. Configuration, tests,
and the Studio entrypoint are separate and do not belong in a Cloud Run build.
