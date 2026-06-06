"""Google Analytics (GA4) for the Streamlit viewer.

Streamlit Community Cloud serves the app behind an auth-bootstrap redirect and
gives us no way to edit the served page <head>, so a static-file patch is neither
reliable nor verifiable there. Instead we inject the gtag tag from a Streamlit
component into the PARENT document: the component iframe is same-origin
(`allow-same-origin`), so its script can append gtag.js to `window.parent`'s
<head> and run it in the real top-level page — correct URL, referrer, and geo.

This runs on every page load (no cold-start gap) and needs no filesystem write.
A guard flag on the parent window makes it idempotent across Streamlit's reruns,
so a visit is counted once, not once per interaction.
"""
from scout import config


def ga_component_html(measurement_id: str | None = None) -> str:
    """HTML to hand to st.components.v1.html(..., height=0). Empty string when
    analytics is disabled (no measurement id)."""
    mid = measurement_id or config.GA_MEASUREMENT_ID
    if not mid:
        return ""
    return (
        "<script>(function(){"
        "var p=window.parent;"
        "if(!p||p.__scoutGA)return;"          # already loaded in this top-level page
        "p.__scoutGA=true;"
        "var d=p.document,s=d.createElement('script');"
        "s.async=true;"
        f"s.src='https://www.googletagmanager.com/gtag/js?id={mid}';"
        "d.head.appendChild(s);"
        "p.dataLayer=p.dataLayer||[];"
        "function gtag(){p.dataLayer.push(arguments);}"
        "p.gtag=gtag;"
        "gtag('js',new Date());"
        f"gtag('config','{mid}');"
        "})();</script>"
    )
