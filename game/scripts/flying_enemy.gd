extends VoidEnemy
class_name FlyingEnemy

@export var fly_speed: float = 110.0
@export var detection_radius: float = 250.0

var start_position: Vector2

func _ready() -> void:
	super._ready()
	start_position = global_position

func _physics_process(delta: float) -> void:
	var player = get_tree().get_first_node_in_group("player")
	if player:
		var dist = global_position.distance_to(player.global_position)
		if dist <= detection_radius:
			# Chase player
			var move_dir = (player.global_position - global_position).normalized()
			velocity = velocity.move_toward(move_dir * fly_speed, 400.0 * delta)
		else:
			# Return to start position
			var move_dir = (start_position - global_position).normalized()
			velocity = velocity.move_toward(move_dir * (fly_speed * 0.5), 200.0 * delta)
	
	move_and_slide()
