# Colombia workspace design

This records the implemented standalone web surface, not visual approval or a
claim of deployment. Mode: **Operate**. Audience: the initially authorized owner.

## Structure

The public entry explains prepare, review and send in three ruled rows. It has a
single login action and a permanent return to Agentic. When configuration is
missing, the real page disables login and explains why; it does not preview a fake
authenticated session.

After authentication, a narrow form pane sits beside a wider reading pane.
Itinerary and Email are separate form modes. Recent operations are plain rows.
Results retain normal text measure, clear day boundaries and exact email content.
The explicit send action appears only with a saved signed draft. Receipt and
uncertain-result states are textual, not decorative progress or invented telemetry.

## Tokens and behavior

| Role | Implementation |
|------|----------------|
| Ground | `#17191b` |
| Form surface | `#202326` |
| Field surface | `#292d31` |
| Primary text | `#f6f0e8` |
| Secondary text | `#bbb9b5` |
| Action / focus | `#ff6975`, with dark `#251317` text |
| Rules | `#484b4e` |
| Typography | System sans; fixed rem scale, 16px base |
| Controls | Native inputs, 46px minimum fields, 48px primary actions |
| Responsive | Form and result stack below 680px; no horizontal workflow canvas |
| Motion | 160ms control feedback only; disabled for reduced motion |

No raster, downloaded imagery, third-party font, copied hub robot or decorative
asset is shipped. Icons are one consistent authored SVG stroke. Generated content
is inserted with `textContent`, never HTML. Final browser capture and independent
finish review remain the parent task's verification boundary.
