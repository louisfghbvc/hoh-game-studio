extends StaticBody2D
class_name BreakableWall

@export var max_hits: int = 3
@export var hits_remaining: int = 3

signal wall_broken()

@onready var color_rect: ColorRect = $ColorRect if has_node("ColorRect") else null

func _ready() -> void:
	hits_remaining = max_hits

func take_damage(amount: int, _source_pos: Vector2 = Vector2.ZERO) -> void:
	hits_remaining -= amount
	
	# Visual feedback: flash white & darken/crack color
	if color_rect:
		var original_color = color_rect.color
		color_rect.color = Color(1.0, 1.0, 1.0, 0.9)
		await get_tree().create_timer(0.06).timeout
		if is_instance_valid(color_rect):
			# Modulate color based on remaining health
			var health_ratio = float(hits_remaining) / float(max_hits)
			color_rect.color = Color(0.45 * health_ratio + 0.1, 0.3 * health_ratio + 0.1, 0.25 * health_ratio + 0.1, 1.0)

	# Spawn Hit Sparks
	var sparks_scene = load("res://scenes/hit_sparks.tscn")
	if sparks_scene:
		var sparks = sparks_scene.instantiate()
		get_parent().add_child(sparks)
		sparks.global_position = global_position

	if hits_remaining <= 0:
		break_wall()

func break_wall() -> void:
	emit_signal("wall_broken")
	queue_free()
