import streamlit as st
import json
import re
from groq import Groq
import truth_seeker_fidelity as fid

st.set_page_config(
    page_title="Sieve Engine: Workbench",
    page_icon="⚖️",
    layout="wide",
)

st.title("⚖️ Sieve Engine: Інтерактивний Аудит Гіпотез")
st.caption("Детерміноване Сито логічного виведення та захисту бізнес-вимог від ерозії")

# ==================== Ініціалізація ====================
client = None
if "GROQ_API_KEY" in st.secrets:
    try:
        client = Groq(api_key=st.secrets["GROQ_API_KEY"])
    except Exception as e:
        st.error(f"Помилка ініціалізації Groq: {e}")
else:
    st.warning("⚠️ Не налаштовано GROQ_API_KEY у вкладці Secrets.")

# Стан сесії
if "graph_state" not in st.session_state:
    st.session_state.graph_state = None
if "analysis_complete" not in st.session_state:
    st.session_state.analysis_complete = False

def clean_json_response(raw_text: str) -> str:
    """Витягує чистий JSON рядок, відкидаючи блок markdown огорож."""
    text = raw_text.strip()
    match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
    if match:
        return match.group(1).strip()
    return text

# ==================== ПАНЕЛЬ ВВЕДЕННЯ ====================
st.subheader("📋 Конфігурація аналізу")

col_model, col_temp = st.columns(2)
with col_model:
    active_model = st.selectbox(
        "Модель Groq:",
        (
            "llama-3.3-70b-versatile",
            "deepseek-r1-distill-llama-70b"
        ),
        index=0
    )

with col_temp:
    temperature = st.slider("Температура LLM (дослідницькість):", 0.0, 1.0, 0.3)

st.divider()

st.subheader("🎯 Гіпотеза для аналізу")
user_hypothesis = st.text_area(
    "Введіть вашу гіпотезу, план або ситуацію (широкий діапазон):",
    value="",
    height=150,
    placeholder="Приклад: Сервіс платежів на Go+Postgres, Redis-кеш для ідемпотентності, p99<50ms, бюджет $400/міс..."
)

st.divider()

st.subheader("🔥 Параметри Red Team атаки")
attack_prompt_default = (
    "Знайди критичну ваду в твердженні. "
    "Запропонуй 3-4 пункти конкретної мутації, яка демонструє реальні ризики. "
    "Зосередься на кількісних метриках та способах їх послаблення."
)

custom_attack_prompt = st.text_area(
    "Кастомний промпт для атаки (залиште порожнім для дефолту):",
    value="",
    height=100,
    placeholder="Введіть свою інструкцію для Red Team аналітика..."
)

attack_prompt = custom_attack_prompt.strip() if custom_attack_prompt.strip() else attack_prompt_default

st.divider()

# ==================== ЗАПУСК АНАЛІЗУ ====================
col_btn_run, col_btn_clear = st.columns([3, 1])

with col_btn_run:
    btn_run_analysis = st.button(
        "▶️ Запустити Повний Аналіз (Decompose + Red Team)",
        type="primary",
        use_container_width=True
    )

with col_btn_clear:
    btn_clear = st.button("🔄 Очистити", use_container_width=True)

if btn_clear:
    st.session_state.graph_state = None
    st.session_state.analysis_complete = False
    st.rerun()

# ==================== ЗАПУСК ОСНОВНОГО ПОТОКУ ====================
if btn_run_analysis:
    if not client:
        st.error("❌ Налаштуйте GROQ_API_KEY у Secrets перед запуском.")
    elif not user_hypothesis.strip():
        st.error("❌ Введіть текст гіпотези перед аналізом.")
    else:
        # КРОК 1: Декомпозиція гіпотези
        with st.spinner("⏳ Крок 1/3: Розкладаю гіпотезу на вузли графу та вимоги..."):
            decomp_prompt = f"""
            Ти — компілятор системного графу рушія Sieve.
            Розклади надану гіпотезу чи ситуацію на строгий JSON:
            1. requirements: список цілей або обов'язкових умов. Кожна має:
               - id: "R1", "R2"...
               - text: короткий опис умови
               - priority: "MUST" або "SHOULD"
               - criteria: список об'єктів з "metric", "comparator", "value", "unit"
            2. nodes: список тверджень/фактів (вузлів графу). Кожен має:
               - node_id: "n-1", "n-2"...
               - kind: "CLAIM"
               - statement: опис твердження
               - quantities: список характеристик (metric, comparator, value, unit)
               - serves: список id вимог, які цей вузол обслуговує
               - q: апріорна довіра (від 0.1 до 1.0)

            Гіпотеза для аналізу:
            "{user_hypothesis}"

            Поверни ВИКЛЮЧНО валідний JSON без зайвих привітань, пояснень та без огорож коду.
            """
            try:
                comp = client.chat.completions.create(
                    model=active_model,
                    messages=[
                        {"role": "system", "content": "Ти строгий генератор структур даних JSON. Тільки валідний JSON."},
                        {"role": "user", "content": decomp_prompt}
                    ],
                    temperature=0.1
                )
                raw_json = clean_json_response(comp.choices[0].message.content)
                parsed_data = json.loads(raw_json)
                
                # Додаємо node_hash детерміновано
                nodes_dict = {}
                for n in parsed_data.get("nodes", []):
                    n["scope"] = n.get("scope", [])
                    n["conditions"] = n.get("conditions", [])
                    n["requires"] = n.get("requires", [])
                    n["node_hash"] = fid.node_hash(n)
                    nodes_dict[n["node_id"]] = n

                st.session_state.graph_state = {
                    "hypothesis": user_hypothesis,
                    "requirements": parsed_data.get("requirements", []),
                    "graph_0": nodes_dict,
                    "graph_n": dict(nodes_dict),
                    "mutations": [],
                    "p_start": 0.85,
                    "p_end": 0.85,
                    "attack_results": []
                }
                st.success("✅ Граф успішно побудовано!")
            except Exception as e:
                st.error(f"❌ Помилка декомпозиції: {e}")
                st.session_state.graph_state = None
        
        # КРОК 2: Розрахунок Fidelity
        if st.session_state.graph_state:
            with st.spinner("⏳ Крок 2/3: Розраховую Fidelity Sieve..."):
                state = st.session_state.graph_state
                fid_res = fid.fidelity(
                    reqs=state["requirements"],
                    graph_0=state["graph_0"],
                    graph_n=state["graph_n"],
                    mutations=state["mutations"],
                    approved=set()
                )
                state["fidelity_result"] = fid_res
                st.success("✅ Fidelity розраховано!")
        
        # КРОК 3: Red Team атака на кожен вузол
        if st.session_state.graph_state:
            with st.spinner("⏳ Крок 3/3: Red Team атакує вузли..."):
                state = st.session_state.graph_state
                attack_results = []
                
                for node_id in list(state["graph_n"].keys()):
                    target_node = state["graph_n"][node_id]
                    attack_query = f"""
                    Ти — Red Team аналітик. Знайди критичну ваду у твердженні:
                    "{target_node['statement']}"
                    Кількісні параметри: {target_node.get('quantities', [])}

                    {attack_prompt}
                    Відповідь має бути лаконічна (2-3 параграфи).
                    """
                    try:
                        resp = client.chat.completions.create(
                            model=active_model,
                            messages=[
                                {"role": "system", "content": "Ти суворий верифікатор систем. Red Team аналітик."},
                                {"role": "user", "content": attack_query}
                            ],
                            temperature=temperature
                        )
                        attack_text = resp.choices[0].message.content
                        attack_results.append({
                            "node_id": node_id,
                            "statement": target_node['statement'],
                            "attack": attack_text
                        })
                    except Exception as err:
                        attack_results.append({
                            "node_id": node_id,
                            "statement": target_node['statement'],
                            "attack": f"⚠️ Помилка атаки: {err}"
                        })
                
                state["attack_results"] = attack_results
                st.success("✅ Red Team аналіз завершено!")
        
        st.session_state.analysis_complete = True
        st.rerun()

# ==================== ВІДОБРАЖЕННЯ РЕЗУЛЬТАТІВ ====================
if st.session_state.analysis_complete and st.session_state.graph_state:
    st.divider()
    st.subheader("📊 Результати Аналізу")
    
    state = st.session_state.graph_state
    fid_res = state.get("fidelity_result")
    
    if fid_res:
        # Метрики
        st.subheader("⚙️ Ключові метрики")
        c1, c2, c3 = st.columns(3)
        c1.metric("P(Успіх) плану", f"{state['p_end']*100:.1f}%", f"{(state['p_end'] - state['p_start'])*100:+.1f}%")
        gate_val = fid_res.gate["result"]
        c2.metric("Сито Здорового Глузду (Gate)", gate_val)
        c3.metric("Alignment Score", f"{fid_res.alignment_score*100:.1f}%")

        st.divider()
        
        if gate_val == "FAIL":
            st.error("❌ План відхилено: виявлено порушення або деградацію MUST-вимог.")
            st.subheader("Причини відхилення:")
            for r in fid_res.gate.get("reasons", []):
                st.markdown(f"- **{r['rule']}** ({r.get('requirement_id', '')}): {r.get('detail', '')}")
        elif gate_val == "PASS":
            st.success("✅ План повністю підтверджено без критичних поступок.")

    # Red Team результати
    st.divider()
    st.subheader("🔥 Red Team Аналіз")
    
    attack_results = state.get("attack_results", [])
    
    if attack_results:
        st.write(f"**Всього атак:** {len(attack_results)}")
        
        # Картки атак у вкладках (табах)
        tabs = st.tabs([f"Вузол {i+1}: {ar['node_id']}" for i, ar in enumerate(attack_results)])
        
        for tab, attack_result in zip(tabs, attack_results):
            with tab:
                st.markdown(f"**Твердження:**")
                st.write(attack_result['statement'])
                
                st.markdown(f"**Red Team Аналіз:**")
                st.info(attack_result['attack'])
                
                # Можна добавити кнопку експорту результату
                if st.button(f"📋 Копіювати для вузла {attack_result['node_id']}", key=f"copy_{attack_result['node_id']}"):
                    st.success(f"Результат для {attack_result['node_id']} готовий до експорту")
    else:
        st.warning("⚠️ Red Team аналіз не виконаний")
    
    # Вимоги та граф
    if fid_res:
        st.divider()
        st.subheader("📋 Вимоги та їх покриття")
        
        for req_item in fid_res.requirements:
            status = req_item["status"]
            color = "green" if status == "PRESERVED" else ("orange" if status == "WEAKENED" else "red")
            req_text = next((r["text"] for r in state["requirements"] if r["id"] == req_item["requirement_id"]), "")
            st.markdown(f"**[{req_item['priority']}] {req_text}** — :{color}[{status}] (Покриття: {req_item['coverage']*100:.0f}%)")
