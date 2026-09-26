def pipe_wall_thickness(P: float, D: float, S: float) -> float:
    if P <= 0 or D <= 0 or S <= 0:
        raise ValueError("P, D, and S must be positive")
    t = P * D / (2 * S)
    return t


if __name__ == "__main__":
    P, D, S = 10.0, 200.0, 138.0
    print("Formula: t = P * D / (2 * S)")
    print(f"Inputs: P = {P} MPa, D = {D} mm, S = {S} MPa")
    t = pipe_wall_thickness(P, D, S)
    print(f"t = {P} * {D} / (2 * {S}) = {t:.3f} mm")
