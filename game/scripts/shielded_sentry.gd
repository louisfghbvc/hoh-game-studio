extends VoidEnemy
class_name ShieldedSentry

@export var shield_facing: int = -1 # -1 for facing left, 1 for facing right

@onready var shield_rect: ColorRect = $ShieldRect if has_node("ShieldRect") else null

func take_damage(amount: int, source_position: Vector2 = Vector2.ZERO) -> void:
	if source_position != Vector2.ZERO:
		# Check if attack comes from the front (blocked by shield)
		var attack_dir = (source_position - global_position).normalized()
		var attack_from_front = (attack_dir.x < 0 and shield_facing < 0) or (attack_dir.x > 0 and shield_facing > 0)
		
		# If attack comes from front and not from above (pogo)
		if attack_from_front and abs(attack_dir.y) < 0.7:
			_trigger_shield_block_effect()
			return

	super.take_damage(amount, source_position)

func _trigger_shield_block_effect() -> void:
	# Flash shield yellow to indicate blocked attack
	if shield_rect:
		var orig = shield_rect.color
		shield_rect.color = Color(1.0, 1.0, 0.2, 1.0)
		await get_tree().create_timer(0.1).timeout
		if is_instance_valid(shield_rect):
			shield_rect.color = orig
