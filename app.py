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

# 1. Ініціалізація клієнта Groq із Secrets
client = None
if "GROQ_API_KEY" in st.secrets:
    try:
        client = Groq(api_key=st.secrets["GROQ_API_KEY"])
    except Exception as e:
        st.error(f"Помилка ініціалізації Groq: {e}")
else:
    st.warning("⚠️ Не налаштовано GROQ_API_KEY у вкладці Secrets.")

# Завантаження даних детермінованого контракту
@st.cache_data
def load_data():
    with open("truth_seeker_fidelity_examples.json", "r", encoding="utf-8") as f:
        data = json.load(f)
    fid_record = next(r for r in data["ledger"] if r["type"] == "FIDELITY")
    concession_records = [r["payload"] for r in data["ledger"] if r["type"] == "CONCESSION"]
    return data["requirements"], fid_record["payload"], concession_records

try:
    reqs, fid, concessions = load_data()

    # Панель верхніх метрик
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

except Exception as e:
    st.error(f"Помилка завантаження файлу прикладів: {e}")

# ==========================================================
# 2. ПОСЛІДОВНЕ ВІКНО: ОБ'ЄКТ АНАЛІЗУ -> ПРОМПТ ДЛЯ АТАКИ
# ==========================================================
st.divider()
st.header("🎯 Генератор Red Team Атак на План")
st.caption("Крок 1: Задайте об'єкт для дослідження. Крок 2: Налаштуйте вектор атаки моделі.")

with st.form("red_team_analysis_form"):
    # КРОК 1: ОБ'ЄКТ АНАЛІЗУ
    st.subheader("Крок 1: Об'єкт для аналізу (Цільовий вузол / План)")
    target_object = st.text_area(
        "Опишіть твердження, архітектурне рішення або фрагмент специфікації:",
        value="API Gateway балансує трафік на 4 інстанси додатку і гарантує витримку пікового навантаження 12 000 rps без деградації бази даних за рахунок Redis-кешу.",
        height=100,
        help="Саме це твердження буде піддано декомпозиції на приховані вразливості."
    )

    # КРОК 2: НАЛАШТУВАННЯ АТАКИ
    st.subheader("Крок 2: Інструкція для атаки (Red Team Attack Prompt)")
    
    col_model, col_temp = st.columns([2, 1])
    with col_model:
        model_name = st.selectbox(
            "Модель міркувань:",
            ("llama-3.3-70b-versatile", "deepseek-r1-distill-llama-70b")
        )
    with col_temp:
        temperature = st.slider("Креативність (Temperature):", 0.0, 1.0, 0.4, 0.1)

    system_role = st.text_input(
        "Роль атакуючого (System Prompt):",
        value="Ти — безжальний Red Team аналітик високонавантажених розподілених систем. Твоя мета — знайти фатальну точку відмови."
    )

    attack_prompt_template = st.text_area(
        "Шаблон інструкції атаки:",
        value="""Здійсни стрес-тест наступного об'єкта:
\"\"\"{target}\"\"\"

Твоє завдання:
1. Виявити 1 критичну вразливість, яка призведе до відмови системи за пікових умов (наприклад: Cache Stampede, вичерпання пулу з'єднань, неконсистентність).
2. Сформулювати чітку контратаку: що саме зламається і як.
3. Оцінити силу доказу за шкалою Likelihood Ratio (LR від 0.1 до 10.0, де LR > 1.0 означає високу ймовірність збою).""",
        height=160
    )

    # Кнопка відправки форми
    submitted = st.form_submit_button("🔥 Запустити атаку на об'єкт", type="primary")

if submitted:
    if not client:
        st.error("Помилка: не знайдено GROQ_API_KEY у Secrets налаштувань Streamlit.")
    elif not target_object.strip():
        st.warning("⚠️ Будь ласка, введіть об'єкт для аналізу в Кроці 1.")
    else:
        with st.spinner("Модель шукає критичні вразливості в наданому об'єкті..."):
            # Підставляємо об'єкт у шаблон інструкції
            final_user_prompt = attack_prompt_template.replace("{target}", target_object)
            
            try:
                response = client.chat.completions.create(
                    model=model_name,
                    messages=[
                        {"role": "system", "content": system_role},
                        {"role": "user", "content": final_user_prompt}
                    ],
                    temperature=temperature,
                    max_tokens=900
                )
                
                st.success("✅ Атаку успішно згенеровано!")
                st.markdown("### 📋 Результати стрес-тесту:")
                st.markdown(response.choices[0].message.content)
                
            except Exception as err:
                st.error(f"Помилка виклику API Groq: {err}")
