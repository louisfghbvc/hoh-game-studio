extends Camera2D
class_name VoidCamera

@export var shake_decay: float = 15.0
@export var max_offset: Vector2 = Vector2(8, 8)

var shake_strength: float = 0.0

func _process(delta: float) -> void:
	if shake_strength > 0:
		shake_strength = lerp(shake_strength, 0.0, shake_decay * delta)
		offset = Vector2(
			randf_range(-shake_strength, shake_strength),
			randf_range(-shake_strength, shake_strength)
		)
	else:
		offset = Vector2.ZERO

func apply_shake(strength: float = 8.0) -> void:
	shake_strength = strength
