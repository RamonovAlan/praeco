# Praeco v0.1.1

This patch release makes owner-provided content a first-class, lower-cost path
and improves image revisions after a rejected visual concept.

## What changed

- Use `/text <post>` to review an already written post without paying for text
  generation.
- From text review, choose **Approve and upload my image** to bypass image
  generation and its cost.
- Image generation receives the exact approved post and evaluates multiple
  semantic angles before selecting a visual metaphor.
- Feedback such as “change the concept”, “start over”, or “try another way” now
  creates a fresh image rather than editing the rejected composition.

Publishing remains approval-first: neither these paths nor automated tests can
publish without the owner's final button for the exact reviewed snapshot.
