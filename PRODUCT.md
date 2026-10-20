# Colombia agents workspace

<!-- impeccable:product-schema 1 -->

## Platform

web

## Stack

Existing Python/FastAPI repository, with a separate FastAPI web application and
plain HTML, CSS and JavaScript. No new frontend dependency installation.

## Users

Students inspecting the implementation offline. A deployed instance requires an
explicit server-side Google owner allowlist; no student is automatically enrolled.

## Product Purpose

Prepare a Colombia itinerary, prepare an email independently or through the
itinerary agent, inspect its exact contents, and explicitly send that email.

## Operating Context

The Agentic hub links to this independent application. This application provides
a fixed return link to the hub. Authentication, state, secrets and delivery remain
independent. The existing Itinerary and Email APIs are separate services.

## Capabilities and Constraints

- Google Web OAuth is separate from Gmail authorization and hub authentication.
- Draft generation never grants permission to send. One explicit send action does.
- API keys, signed drafts and OAuth tokens remain server-side.
- Production persistence requires a dedicated, explicitly configured Firestore database.
- The initial interface is structured forms, not unrestricted agent chat.
- Runnable UI deployment, OAuth client/origin and Firestore provisioning require
  separate operator configuration and live acceptance.
- Calendar, live MCP retrieval and production login are not claimed by this build.

## Accessibility & Inclusion

Spanish interface, keyboard access, visible focus, readable contrast, responsive
composition and reduced-motion support.
