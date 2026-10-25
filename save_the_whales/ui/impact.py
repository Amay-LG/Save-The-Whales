"""Original explanatory illustration and sourced context, separate from results."""
from pathlib import Path


def render_story():
    import streamlit.components.v1 as components
    components.html((Path(__file__).parent/'assets'/'ocean_story.html').read_text(), height=385, scrolling=False)


def render_facts():
    import streamlit as st
    st.subheader('Why a little less speed matters')
    st.caption('Real-world context from independent sources. These are not outcomes produced by this demo.')
    a,b,c=st.columns(3)
    with a:
        st.markdown('### 10 knots')
        st.write('Blue Whales and Blue Skies encourages participating ships to travel at 10 knots or less in its vessel-speed-reduction zones.')
        st.markdown('[Program overview ↗](https://bluewhalesblueskies.org/)')
    with b:
        st.markdown('### 481,273 nm')
        st.write('Participating vessels traveled this distance at whale-safer speed during the program’s 2025 season. Small speed decisions can add up over many voyages.')
        st.markdown('[2025 program results ↗](https://bluewhalesblueskies.org/impact/)')
    with c:
        st.markdown('### Many go unseen')
        st.write('The Marine Mammal Commission notes that whale deaths can go undetected when offshore carcasses sink rather than wash ashore.')
        st.markdown('[Marine Mammal Commission ↗](https://www.mmc.gov/priority-topics/vessel-strikes/)')
    st.caption('Our question: can a simulated route or speed change reduce modeled whale exposure with an acceptable delay? The paired comparison measures that tradeoff; it does not estimate real whales saved.')
