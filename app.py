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
    try:
        client = Groq(api_key=st.secrets["GROQ_API_KEY"])
    except Exception as e:
        st.error(f"Помилка ініціалізації Groq: {e}")
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

except FileNotFoundError:
    st.error("Файл 'truth_seeker_fidelity_examples.json' не знайдено в репозиторії.")

# 2. Інтерактивний Red Team аудит через Groq
st.divider()
st.subheader("🤖 Живий Red Team аудит (Groq Reasoning)")

col_model, col_temp = st.columns([2, 1])
with col_model:
    model_name = st.selectbox(
        "Модель міркувань:",
        ("llama-3.3-70b-versatile", "deepseek-r1-distill-llama-70b")
    )
with col_temp:
    temperature = st.slider("Temperature (креативність):", 0.0, 1.0, 0.6, 0.1)

# Поле системного промпта
default_system_prompt = "Ти — строгий верифікатор та Red Team аналітик надійності архітектурних систем."
system_prompt = st.text_area(
    "Системний промпт (роль та обмеження моделі):",
    value=default_system_prompt,
    height=70
)

# Поле твердження вузла
target_statement = st.text_input(
    "Твердження вузла для атаки (Target Node):",
    value="API-шар масштабується горизонтально до 12 000 rps"
)

# Поле шаблону промпта завдання
default_user_prompt = (
    "Проаналізуй твердження плану: \"{statement}\".\n\n"
    "Твоя задача — виявити приховані вади, вузькі місця або хибні припущення.\n"
    "Сформулюй структуровану відповідь:\n"
    "1. Назва атаки (лаконічно).\n"
    "2. Суть вразливості (чому це не спрацює за пікових умов).\n"
    "3. Оцінка Likelihood Ratio (LR від 0.1 до 10.0, де >1 підтримує атаку, <1 спростовує)."
)
user_prompt_template = st.text_area(
    "Шаблон промпта завдання (використовуйте {statement} для підстановки твердження):",
    value=default_user_prompt,
    height=180
)

if st.button("Згенерувати атаку"):
    if not client:
        st.error("Помилка: клієнт Groq не налаштований у Secrets.")
    elif "{statement}" not in user_prompt_template:
        st.warning("⚠️ У шаблоні промпта відсутній маркер `{statement}` для підстановки твердження.")
    else:
        with st.spinner("Модель виконує декомпозицію та стрес-тест..."):
            final_prompt = user_prompt_template.format(statement=target_statement)

            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": final_prompt}
            ]

            try:
                # Виклик API із захистом від збою
                completion = client.chat.completions.create(
                    model=model_name,
                    messages=messages,
                    temperature=temperature,
                    max_tokens=1024
                )
                st.markdown("### 🎯 Результат атаки:")
                st.info(completion.choices[0].message.content)
            except Exception as err:
                st.error(f"Помилка виклику моделі Groq: {err}")
