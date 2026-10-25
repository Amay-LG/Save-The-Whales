"""Compact, sourced product context. No illustrations presented as model results."""
import base64
from pathlib import Path


def render_hero():
    import streamlit as st
    whale = base64.b64encode((Path(__file__).parent/'assets'/'whale.svg').read_bytes()).decode()
    st.markdown(f'''<style>
    .save-hero{{position:relative;padding:28px 0 30px;overflow:hidden;min-height:190px;border-bottom:1px solid #315466;margin-bottom:18px}}
    .save-hero:after{{content:"";position:absolute;right:0;top:-10px;width:52%;height:215px;background:url("data:image/svg+xml;base64,{whale}") center/contain no-repeat;opacity:.095;pointer-events:none}}
    .save-hero h1{{position:relative;z-index:1;font-size:clamp(34px,4vw,52px)!important;line-height:1.15;margin:8px 0 14px;font-weight:550}}
    .save-hero p{{position:relative;z-index:1;max-width:540px;color:#bed3dc;line-height:1.7;font-size:16px}}
    .save-hero .kicker{{color:#8ddacc;font-size:10px;letter-spacing:2px}}
    .save-hero a{{color:#b3e9df;text-decoration:none;font-size:12px;margin-right:18px}}
    .impact-item{{padding:4px 18px 8px 0}}
    .impact-number{{font-size:10px;letter-spacing:2px;color:#8ddacc;margin:0 0 12px}}
    @media(max-width:700px){{.save-hero:after{{width:70%;opacity:.065}}.save-hero p{{font-size:14px}}}}
    </style>
    <section class="save-hero">
      <div class="kicker">SHIPPING ROUTES CROSS WHALE HABITAT</div>
      <h1>Save the Whales</h1>
      <p>Ship strikes threaten large whales, and some deaths are never observed.<br>Explore how a small change of speed or course can change the modeled risk.</p>
      <p style="font-size:13px;color:#8ddacc;margin-top:10px">A shared ocean. A little more room to live.</p>
      <a href="#santa-barbara-simulated-passage">Explore the simulation ↓</a>
      <a href="#why-this-matters">Why this matters ↓</a>
    </section>''', unsafe_allow_html=True)


def render_facts():
    import streamlit as st
    st.markdown('<div id="why-this-matters"></div>', unsafe_allow_html=True)
    st.subheader('Why this matters')
    a,b,c = st.columns(3)
    with a:
        st.markdown('<div class="impact-number">01 / SPEED</div>', unsafe_allow_html=True)
        st.markdown('#### Speed changes the risk')
        st.markdown('**10 knots or less.** NOAA encourages slower travel to reduce the risk of serious injury when vessels encounter whales.')
        st.write('Slower travel can reduce collision severity. The outcome still depends on vessel size and the circumstances of the encounter.')
        st.markdown('[NOAA vessel-strike guidance ↗](https://www.fisheries.noaa.gov/national/endangered-species-conservation/vessel-strikes)')
    with b:
        st.markdown('<div class="impact-number">02 / VISIBILITY</div>', unsafe_allow_html=True)
        st.markdown('#### Not every strike is seen')
        st.markdown('**The recorded toll is incomplete.** Offshore whale carcasses can sink instead of reaching shore, leaving deaths undetected.')
        st.write('This is why reported sightings and their uncertainty matter. Our simulation explores decisions from imperfect evidence, rather than assuming the ocean is clear.')
        st.markdown('[Marine Mammal Commission ↗](https://www.mmc.gov/priority-topics/vessel-strikes/)')
    with c:
        st.markdown('<div class="impact-number">03 / DECISIONS</div>', unsafe_allow_html=True)
        st.markdown('#### Make the tradeoff visible')
        st.markdown('**85% fleet cooperation.** Blue Whales and Blue Skies reported this cooperation rate among participating fleets in its 2025 season.')
        st.write('Our question is practical: how much simulated whale exposure can a route or speed change avoid, and what delay does it add? Compare both outcomes below.')
        st.markdown('[2025 program results ↗](https://bluewhalesblueskies.org/impact/)')
    st.caption('Independent real-world context. Demo outputs are simulated research proxies, not deaths avoided. No real ship is controlled.')
