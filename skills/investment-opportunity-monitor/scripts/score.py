"""Motor de score de risco generico, dirigido por config.

Nenhuma logica especifica de classe de ativo vive aqui -- os componentes,
pesos e regras vem inteiramente do bloco 'risk_score' do config JSON da
classe (config/defi.json, config/stocks.json, etc). Para adicionar uma
classe nova sem tocar neste arquivo, basta descrever o score dela usando os
tipos de transform ja suportados abaixo.

Politica de dado faltante: se um componente nao pode ser calculado (campo
ausente no record, ou referencia nula), ele e excluido do calculo daquela
pool/ativo especifica e o peso e redistribuido entre os componentes que
sobraram (missing_field_policy = exclude_and_renormalize). O record final
carrega '_missing_components' listando o que faltou, para transparencia.
Nunca se inventa um valor no lugar de um dado ausente.
"""

import math


def _get_path(record: dict, field: str):
    return record.get(field)


def _transform_log10_minmax(value, dataset_values, _comp):
    if value is None or value <= 0:
        return None
    logs = [math.log10(v) for v in dataset_values if v is not None and v > 0]
    if not logs:
        return None
    lo, hi = min(logs), max(logs)
    if hi == lo:
        return 1.0
    return (math.log10(value) - lo) / (hi - lo)


def _transform_minmax_capped(value, _dataset_values, comp):
    if value is None:
        return None
    cap = comp.get("cap")
    if not cap or cap <= 0:
        return None
    return max(0.0, min(1.0, value / cap))


def _transform_categorical_rules(_value, _dataset_values, comp, record=None):
    derive_from = comp.get("derive_from", [])
    context = {k: record.get(k) for k in derive_from} if record else {}
    for rule in comp.get("rules", []):
        when = rule.get("when", {})
        if all(context.get(k) == v for k, v in when.items()):
            return rule.get("score")
    return None


def _transform_relative_deviation_inverse(_value, _dataset_values, comp, record=None):
    value_field = comp.get("value_field")
    reference_field = comp.get("reference_field")
    current = record.get(value_field) if record else None
    reference = record.get(reference_field) if record else None
    if current is None or reference is None or reference == 0:
        return None
    deviation = abs(current - reference) / abs(reference)
    cap = comp.get("cap_deviation", 1.0)
    deviation = min(deviation, cap)
    return 1.0 - (deviation / cap)


TRANSFORMS = {
    "log10_minmax": _transform_log10_minmax,
    "minmax_capped": _transform_minmax_capped,
    "categorical_rules": _transform_categorical_rules,
    "relative_deviation_inverse": _transform_relative_deviation_inverse,
}


def compute_scores(records: list[dict], risk_score_config: dict) -> list[dict]:
    components = risk_score_config["components"]

    dataset_cache = {}
    for comp in components:
        field = comp["field"]
        dataset_cache[field] = [_get_path(r, field) for r in records]

    scored = []
    for record in records:
        weighted_sum = 0.0
        total_weight_used = 0.0
        missing = []
        breakdown = {}

        for comp in components:
            field = comp["field"]
            transform_name = comp["transform"]
            transform_fn = TRANSFORMS.get(transform_name)
            if transform_fn is None:
                raise ValueError(f"Transform desconhecido no config: {transform_name}")

            raw_value = _get_path(record, field)
            if transform_name in ("categorical_rules", "relative_deviation_inverse"):
                normalized = transform_fn(raw_value, dataset_cache[field], comp, record=record)
            else:
                normalized = transform_fn(raw_value, dataset_cache[field], comp)

            if normalized is None:
                missing.append(comp.get("label", field))
                continue

            weight = comp["weight"]
            weighted_sum += normalized * weight
            total_weight_used += weight
            breakdown[comp.get("label", field)] = round(normalized * 100, 1)

        risk_score = round((weighted_sum / total_weight_used) * 100, 1) if total_weight_used > 0 else None

        record_out = dict(record)
        record_out["risk_score"] = risk_score if risk_score is not None else "indisponivel"
        record_out["_score_breakdown"] = breakdown
        record_out["_missing_components"] = missing
        scored.append(record_out)

    return scored
