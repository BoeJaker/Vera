idf_component_register(SRCS "vera_mesh_main.c"
                       INCLUDE_DIRS "."
                       REQUIRES esp_wifi esp_http_client nvs_flash json driver esp_event)
