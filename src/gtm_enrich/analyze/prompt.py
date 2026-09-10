"""Prompt construction, kept separate from transport so it can be diffed and tested.

The system block is deliberately identical for every domain in a run: it is the
cached prefix, so a 50-account batch pays for the ICP definition once. Everything
that varies per account goes in the user turn, after the cache breakpoint.
"""

from __future__ import annotations

from ..config import IcpProfile
from ..models import ScrapedPage

SYSTEM_TEMPLATE = """\
You are a GTM research analyst. You read a company's homepage and answer a fixed \
set of questions about it so the answers can be written into a CRM.

{icp_block}

Rules:
- Judge only what is on the page. You may recognize the company; ignore what you \
know about it and score the evidence in front of you.
- Never invent a buying signal. An empty list is a correct answer.
- Marketing copy overstates. "Trusted by thousands" is not an enterprise signal; \
named Fortune 500 logos, SOC 2, SSO, and procurement language are.
- Quotes in `evidence` must appear verbatim on the page.
- If the page is thin, a placeholder, or mostly JavaScript, say so in \
`icp_fit_rationale` and set `confidence` below 0.4.
"""

USER_TEMPLATE = """\
Analyze the homepage of {domain}.

<page_metadata>
url: {final_url}
title: {title}
meta_description: {meta_description}
</page_metadata>

<structural_signals>
These were read directly from the page's HTML and links. They are facts, not \
guesses -- use them, and do not contradict them.
detected_vendors: {tech_signals}
{page_signals}
</structural_signals>

<homepage_markdown>
{markdown}
</homepage_markdown>
"""


def build_system(icp: IcpProfile) -> str:
    return SYSTEM_TEMPLATE.format(icp_block=icp.as_prompt_block())


def build_user(page: ScrapedPage, page_signals: dict[str, bool]) -> str:
    signal_lines = "\n".join(f"{k}: {str(v).lower()}" for k, v in sorted(page_signals.items()))
    return USER_TEMPLATE.format(
        domain=page.domain,
        final_url=page.final_url,
        title=page.title or "(none)",
        meta_description=page.meta_description or "(none)",
        tech_signals=", ".join(page.tech_signals) or "(none detected)",
        page_signals=signal_lines,
        markdown=page.markdown,
    )
