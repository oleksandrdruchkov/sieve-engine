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

# 1. Ініціалізація клієнта Groq
client = None
if "GROQ_API_KEY" in st.secrets:
    try:
        client = Groq(api_key=st.secrets["GROQ_API_KEY"])
    except Exception as e:
        st.error(f"Помилка ініціалізації Groq: {e}")
else:
    st.warning("⚠️ Не налаштовано GROQ_API_KEY у вкладці Secrets.")

# Зберігаємо стан графу та вимог у пам'яті сесії Streamlit
if "graph_state" not in st.session_state:
    st.session_state.graph_state = None

def clean_json_response(raw_text: str) -> str:
    """Витягує чистий JSON рядок, відкидаючи блок markdown огорож."""
    text = raw_text.strip()
    match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
    if match:
        return match.group(1).strip()
    return text

# ==========================================================
# КРОК 1: ВВІД ГІПОТЕЗИ ТА ДЕКОМПОЗИЦІЯ В СТРУКТУРОВАНИЙ ГРАФ
# ==========================================================
st.subheader("1. Початкова гіпотеза або план (Об'єкт аналізу)")

default_hypothesis = (
    "Сервіс обробки платежів побудовано на Go та Postgres. "
    "Для витримки навантаження 10000 rps використовується Redis-кеш для ідемпотентності, "
    "а час відповіді p99 не перевищує 50ms при бюджеті до $400 на місяць."
)

user_hypothesis = st.text_area(
    "Введіть текст довільної гіпотези, плану або ситуації для перевірки:",
    value=default_hypothesis,
    height=120
)

col_model_select, col_btn = st.columns([1, 1])
with col_model_select:
    active_model = st.selectbox(
        "Модель Groq для міркувань:",
        (
            "llama-3.3-70b-versatile",
            "deepseek-r1-distill-llama-70b"
        ),
        index=0
    )

with col_btn:
    st.write("")
    st.write("")
    btn_decompose = st.button("🔨 Сформувати Baseline (Граф 0)", type="primary")

if btn_decompose:
    if not client:
        st.error("Налаштуйте GROQ_API_KEY у Secrets перед запуском.")
    elif not user_hypothesis.strip():
        st.warning("Введіть текст гіпотези перед декомпозицією.")
    else:
        with st.spinner("LLM розкладає гіпотезу на вузли графу та вимоги (Decompose)..."):
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
                    "p_end": 0.85
                }
                st.success("✅ Baseline зафіксовано! Граф успішно побудовано.")
                st.rerun()
            except Exception as e:
                st.error(f"Помилка декомпозиції гіпотези: {e}")

# ==========================================================
# КРОК 2: ВІДОБРАЖЕННЯ ДАШБОРДУ (FIDELITY SIEVE)
# ==========================================================
if st.session_state.graph_state:
    st.divider()
    state = st.session_state.graph_state
    
    # Вираховуємо Fidelity на льоту детермінованим ядром
    fid_res = fid.fidelity(
        reqs=state["requirements"],
        graph_0=state["graph_0"],
        graph_n=state["graph_n"],
        mutations=state["mutations"],
        approved=set()
    )
    
    # Метрики
    c1, c2, c3 = st.columns(3)
    c1.metric("P(Успіх) плану", f"{state['p_end']*100:.1f}%", f"{(state['p_end'] - state['p_start'])*100:+.1f}%")
    gate_val = fid_res.gate["result"]
    c2.metric("Сито Здорового Глузду (Gate)", gate_val)
    c3.metric("Alignment Score", f"{fid_res.alignment_score*100:.1f}%")

    if gate_val == "FAIL":
        st.error("❌ План відхилено: виявлено порушення або деградацію MUST-вимог.")
        for r in fid_res.gate.get("reasons", []):
            st.write(f"- **{r['rule']}** ({r.get('requirement_id', '')}): {r.get('detail', '')}")
    elif gate_val == "PASS":
        st.success("✅ План повністю підтверджено без критичних поступок.")

    # Деталізація вимог
    with st.expander("Переглянути вимоги та вузли графу", expanded=True):
        for req_item in fid_res.requirements:
            status = req_item["status"]
            color = "green" if status == "PRESERVED" else ("orange" if status == "WEAKENED" else "red")
            req_text = next((r["text"] for r in state["requirements"] if r["id"] == req_item["requirement_id"]), "")
            st.markdown(f"**[{req_item['priority']}] {req_text}** — :{color}[{status}] (Покриття: {req_item['coverage']*100:.0f}%)")

    # ==========================================================
    # КРОК 3: RED TEAM АТАКА НА ОБРАНИЙ ВУЗОЛ
    # ==========================================================
    st.divider()
    st.subheader("2. Red Team атака на один із вузлів")

    available_nodes = list(state["graph_n"].keys())
    if available_nodes:
        selected_node_id = st.selectbox(
            "Оберіть твердження для атаки:",
            available_nodes,
            format_func=lambda x: f"{x}: {state['graph_n'][x]['statement']}"
        )
        
        target_node = state["graph_n"][selected_node_id]

        if st.button("🔥 Атакувати обраний вузол через Groq"):
            with st.spinner("Red Team шукає критичні вразливості..."):
                attack_prompt = f"""
                Ти — Red Team аналітик. Знайди критичну ваду у твердженні:
                "{target_node['statement']}"
                Кількісні параметри: {target_node.get('quantities', [])}

                Запропонуй мутацію (послаблення або спростування), яка покаже реальні ризики.
                Опиши атаку лаконічно (до 4 пунктів).
                """
                try:
                    resp = client.chat.completions.create(
                        model=active_model,
                        messages=[
                            {"role": "system", "content": "Ти суворий верифікатор систем."},
                            {"role": "user", "content": attack_prompt}
                        ],
                        temperature=0.3
                    )
                    
                    st.info(resp.choices[0].message.content)
                    
                    # Мутація: послаблюємо першу кількісну метрику для тесту Сита
                    mutated_node = json.loads(json.dumps(target_node))
                    if mutated_node.get("quantities"):
                        val = mutated_node["quantities"][0].get("value")
                        if isinstance(val, (int, float)):
                            mutated_node["quantities"][0]["value"] = val * 0.5
                        mutated_node["node_hash"] = fid.node_hash(mutated_node)
                        
                        state["graph_n"][selected_node_id] = mutated_node
                        state["mutations"].append({
                            "type": "MUTATION",
                            "apply_status": "APPLIED",
                            "payload": {
                                "mutation_type": "RETYPE_EDGE",
                                "nodes_removed": [target_node],
                                "nodes_added": [mutated_node]
                            }
                        })
                        state["p_end"] = max(0.1, state["p_end"] - 0.25)
                        st.rerun()
                except Exception as err:
                    st.error(f"Помилка при виклику атаки: {err}")
