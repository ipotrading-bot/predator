import streamlit as st
import os

st.title("Predator App")
st.write("Welcome to the Predator dashboard.")

# Running the dashboard
exec(open("ui/dashboard.py").read())
