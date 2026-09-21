import json
import streamlit as st
from groq import Groq

st.set_page_config(
    page_title="Sieve Engine",
    page_icon="⚖️",
    layout="wide",
)

st.title("⚖️ Sieve Engine: Фінальний Контракт")
st.caption("Детермінований аудит інженерної надійності та відповідності цілям")

# 1. Ініціалізація клієнта Groq із секретів Streamlit
client = None
if "GROQ_API_KEY" in st.secrets:
    client = Groq(api_key=st.secrets["GROQ_API_KEY"])
else:
    st.warning("⚠️ Не налаштовано GROQ_API_KEY у вкладці Secrets.")

@st.cache_data
def load_data():
    with open("truth_seeker_fidelity_examples.json", "r", encoding="utf-8") as f:
        data = json.load(f)
    fid_record = next(r for r in data["ledger"] if r["type"] == "FIDELITY")
    concession_records = [r["payload"] for r in data["ledger"] if r["type"] == "CONCESSION"]
    return data["requirements"], fid_record["payload"], concession_records

try:
    reqs, fid, concessions = load_data()

    # Верхній блок метрик
    col1, col2, col3 = st.columns(3)
    p = fid.get("p_trajectory", {"start": 0.0, "end": 0.0})
    gate = fid["gate"]["result"]
    score = fid["alignment_score"]

    col1.metric("P(Успіх) після атак", f"{p['end']*100:.1f}%", f"{(p['end'] - p['start'])*100:+.1f}%")
    col2.metric("Сито Здорового Глузду (Gate)", gate)
    col3.metric("Alignment Score (SHOULD/COULD)", f"{score*100:.1f}%")

    if gate == "FAIL":
        st.error("❌ План відхилено: виявлено неприпустимі компроміси MUST-вимог.")
        for r in fid["gate"]["reasons"]:
            st.write(f"- **{r['rule']}** ({r.get('requirement_id', '')}): {r.get('detail', '')}")
    elif gate == "PASS":
        st.success("✅ План повністю відповідає цілям без деградації вимог.")
    else:
        st.warning(f"⚠️ Статус плану: {gate}")

    st.divider()

    # Деталізація вимог
    st.subheader("Вимоги та покриття")
    for item in fid["requirements"]:
        req_def = next(r for r in reqs if r["id"] == item["requirement_id"])
        status = item["status"]
        color = "green" if status == "PRESERVED" else ("orange" if status == "WEAKENED" else "red")

        with st.expander(f"[{item['priority']}] {req_def['text']} — :{color}[{status}]"):
            st.write(f"**Покриття вимоги:** {item['coverage']*100:.1f}%")
            st.write(f"**Вузли графу:** {', '.join(item['serving_nodes']) if item['serving_nodes'] else 'Немає'}")
            
            related_concs = [c for c in concessions if c["requirement_id"] == item["requirement_id"]]
            if related_concs:
                st.write("**Зафіксовані поступки (Concessions):**")
                for c in related_concs:
                    dp = f" (ΔP: {c['delta_p']:+.2f})" if "delta_p" in c else ""
                    st.markdown(f"- `{c['kind']}`: effect `{c['effect_on_requirement']}`{dp}")

    st.divider()

    # 2. Живий Red Team аудит через Groq
    st.subheader("🤖 Живий Red Team аудит (Groq Reasoning)")
    target_statement = st.text_input("Введіть твердження вузла для атаки:", "API-шар масштабується горизонтально до 12 000 rps")
    
    model_name = st.selectbox(
        "Модель міркувань:",
        ("deepseek-r1-distill-llama-70b", "llama-3.3-70b-versatile")
    )

    if st.button("Згенерувати атаку"):
        if not client:
            st.error("Помилка: клієнт Groq не налаштований у Secrets.")
        else:
            with st.spinner("Модель шукає критичні вразливості..."):
                prompt = f"""
                Ти — Red Team аналітик рушія Sieve.
                Твоя задача — виявити приховані вади, вузькі місця або хибні припущення у твердженні плану:
                "{target_statement}"

                Сформулюй структуровану відповідь:
                1. Назва атаки (лаконічно).
                2. Суть вразливості (чому це не спрацює за пікових умов).
                3. Оцінка Likelihood Ratio (LR від 0.1 до 10.0, де >1 підтримує атаку).
                """
                
                completion = client.chat.completions.create(
                    model=model_name,
                    messages=[
                        {"role": "system", "content": "Ти строгий верифікатор архітектурних рішень."},
                        {"role": "user", "content": prompt}
                    ],
                    temperature=0.2,
                    max_tokens=600
                )
                
                st.markdown("### 🎯 Результат атаки:")
                st.info(completion.choices[0].message.content)

except FileNotFoundError:
    st.error("Файл 'truth_seeker_fidelity_examples.json' не знайдено в репозиторії.")
