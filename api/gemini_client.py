import streamlit as st
import google.generativeai as genai

def get_gemini_client():
    api_key = st.secrets["GEMINI_API_KEY"]
    genai.configure(api_key=api_key)
    return genai.GenerativeModel('gemini-1.5-flash')
