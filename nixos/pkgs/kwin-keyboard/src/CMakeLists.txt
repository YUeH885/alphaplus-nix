cmake_minimum_required(VERSION 3.16)
project(AlphaPlusKWinKeyboard LANGUAGES CXX)

# KWinTargets.cmake exports Qt6::DBus and Qt6::Widgets without finding them in KWinConfig.cmake.
find_package(Qt6 REQUIRED COMPONENTS DBus Widgets)
find_package(KWin REQUIRED)

add_library(alphaplus-kwin-keyboard MODULE keyboard.cpp)
set_target_properties(alphaplus-kwin-keyboard PROPERTIES AUTOMOC ON PREFIX "")
target_link_libraries(alphaplus-kwin-keyboard PRIVATE KWin::kwin)
install(TARGETS alphaplus-kwin-keyboard DESTINATION "${KWIN_PLUGIN_DIR}")
