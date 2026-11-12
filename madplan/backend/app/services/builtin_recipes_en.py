"""The steps of the built-in recipes in English, by Danish title.

Ingredient names stay Danish everywhere, as they are what gets matched against Danish offers. A
test checks that every built-in recipe is here, with the same number of steps.
"""

from __future__ import annotations

STEPS_EN: dict[str, tuple[str, ...]] = {
    "Spaghetti bolognese": (
        "Chop the onion and garlic, and grate the carrots.",
        "Brown the meat in oil in a pot, and add the vegetables.",
        "Add the chopped tomatoes, tomato purée and oregano, and let it simmer for 25 minutes.",
        "Cook the spaghetti according to the package, and serve with the meat sauce.",
    ),
    "Lasagne": (
        "Brown the meat and chopped onion, add the tomatoes and oregano, and let it simmer for 15 minutes.",
        "Melt the butter, stir in the flour, and whisk in the milk a little at a time to make a smooth sauce.",
        "Layer meat sauce, sheets and white sauce in a dish, and finish with sauce and cheese.",
        "Bake at 200 °C for 35 minutes, and let it rest for 10 minutes.",
    ),
    "Kylling i karry med ris": (
        "Cook the rice according to the package.",
        "Cut the chicken and vegetables into bite-sized pieces.",
        "Fry the chicken golden in oil, add the onion, pepper and curry, and fry for 2 minutes.",
        "Add the coconut milk, and let it simmer for 10 minutes. Serve with the rice.",
    ),
    "Tacos med oksekød": (
        "Brown the meat, add the seasoning and a little water, and let it simmer for 5 minutes.",
        "Slice the tomatoes, cucumber and lettuce.",
        "Warm the tortillas, and let everyone build their own tacos.",
    ),
    "Stegt flæsk med persillesovs": (
        "Boil the potatoes until tender.",
        "Bake or fry the pork until crisp, turning it along the way.",
        "Make a sauce from butter, flour and milk, and stir in the parsley. Serve it all together.",
    ),
    "Frikadeller med kartofler og brun sovs": (
        "Mix the meat, grated onion, egg, flour and milk. Season with salt and pepper.",
        "Boil the potatoes and carrots.",
        "Shape the meatballs, and fry them in butter for 5 minutes on each side.",
        "Make a sauce from the pan juices with a little water and flour, and serve.",
    ),
    "Kyllingefrikadeller med ovnkartofler": (
        "Cut the potatoes into wedges, toss them in oil, and bake at 200 °C for 35 minutes.",
        "Mix the meat, grated onion and egg, and fry the patties golden.",
        "Grate the cucumber, mix it with the yoghurt, and serve as a dressing.",
    ),
    "Bagt laks med kartofler og broccoli": (
        "Boil the potatoes until tender.",
        "Put the salmon in a dish, drizzle with oil and lemon, and bake at 180 °C for 15-18 minutes.",
        "Steam the broccoli for 4 minutes, and serve with crème fraîche.",
    ),
    "Fiskefilet med kartofler og citron": (
        "Boil the potatoes and carrots.",
        "Fry the fish fillets golden in butter, 3-4 minutes on each side.",
        "Serve with lemon, while it is all hot.",
    ),
    "Torsk i fad med grøntsager": (
        "Boil the potatoes until tender.",
        "Slice the leeks and carrots, and put them in a dish with the cream.",
        "Put the cod on top, squeeze lemon over it, and bake at 190 °C for 20 minutes.",
    ),
    "Pasta med rejer og citron": (
        "Cook the pasta.",
        "Sauté the garlic, add the cream and a little lemon juice, and let it reduce a little.",
        "Stir in the shrimp, spinach and pasta, and heat through.",
    ),
    "Svinemørbrad med champignonsovs": (
        "Boil the potatoes until tender.",
        "Slice the tenderloin, and fry it in butter. Take it out of the pan.",
        "Fry the onion and mushrooms, add the cream, and let the sauce reduce.",
        "Put the meat back in, and heat through.",
    ),
    "Koteletter i fad med ris": (
        "Cook the rice.",
        "Brown the chops in a pan, and put them in a dish.",
        "Fry the onion and mushrooms, add the cream and paprika, and pour it over the meat.",
        "Bake at 180 °C for 25 minutes.",
    ),
    "Medister med stuvet hvidkål": (
        "Boil the potatoes until tender.",
        "Slice the cabbage, and boil it for 5 minutes.",
        "Make a white sauce from butter, flour and milk, and stir in the cabbage.",
        "Fry the sausage golden, and serve.",
    ),
    "Pølser med kartoffelmos": (
        "Boil the potatoes, and mash them with warm milk and butter.",
        "Fry the sausages, and serve with the mash and pickled cucumber.",
    ),
    "Hjemmelavet burger": (
        "Shape four patties, and fry them for 3-4 minutes on each side. Add the cheese at the end.",
        "Toast the buns, and slice the vegetables.",
        "Build the burgers with mayonnaise and greens.",
    ),
    "Pita med kylling og tzatziki": (
        "Grate the cucumber, and stir it with the yoghurt and garlic.",
        "Fry the chicken in strips with cumin.",
        "Warm the pitas, and fill them with chicken, greens and tzatziki.",
    ),
    "Kyllingelår med rodfrugter i ovn": (
        "Cut the vegetables into large pieces, and toss them in oil and thyme.",
        "Put the chicken thighs on top in a roasting tin.",
        "Bake at 200 °C for 45 minutes, until the meat comes off the bone.",
    ),
    "Helstegt kylling med salat": (
        "Fill the chicken with lemon, and rub it with butter, salt and pepper.",
        "Roast at 200 °C for about 75 minutes, with potatoes in the tin.",
        "Let the chicken rest for 10 minutes, and serve with salad.",
    ),
    "Gullasch med kartoffelmos": (
        "Brown the meat in a pot, and add the onion, pepper and paprika.",
        "Add the tomatoes and 2 dl of water, and let it simmer under a lid for 75 minutes.",
        "Boil the potatoes, and mash them with milk and butter.",
    ),
    "Flæskesteg med rødkål": (
        "Score the rind, rub it with salt, and roast at 200 °C for 90 minutes. Turn up the heat at the end, so that the rind crisps.",
        "Boil the potatoes.",
        "Slice the cabbage, and let it simmer with vinegar and sugar for 30 minutes.",
    ),
    "Chili con carne": (
        "Brown the meat with the onion and garlic.",
        "Add the spices, tomatoes and beans, and let it simmer for 30 minutes.",
        "Cook the rice, and serve.",
    ),
    "Vegetarisk chili sin carne": (
        "Sauté the onion and pepper.",
        "Add the spices, tomatoes and beans, and let it simmer for 25 minutes.",
        "Serve with rice.",
    ),
    "Linsesuppe med flûte": (
        "Sauté the onion and carrots with curry.",
        "Add the lentils, tomatoes, stock and 1 litre of water.",
        "Boil for 20 minutes, and blend a little if you like. Serve with bread.",
    ),
    "Kikærtecurry med spinat": (
        "Sauté the onion and spices.",
        "Add the chickpeas, tomatoes and coconut milk, and let it simmer for 15 minutes.",
        "Stir in the spinach, and serve with rice.",
    ),
    "Pasta med grøntsager og feta": (
        "Cook the pasta.",
        "Fry the vegetables in oil for 8 minutes.",
        "Stir in the pasta, and crumble the feta over it.",
    ),
    "Hjemmelavet pizza": (
        "Roll out the dough, and spread it with tomato and oregano.",
        "Top with cheese, ham and mushrooms.",
        "Bake at 225 °C for 12-15 minutes.",
    ),
    "Omelet med grøntsager": (
        "Beat the eggs, and season them.",
        "Sauté the vegetables in butter, and pour the eggs over them.",
        "Sprinkle with cheese, and let the omelette set over a low heat. Serve with rye bread.",
    ),
    "Kylling med broccoli og flødeost": (
        "Fry the chicken in butter with garlic until cooked through.",
        "Steam the broccoli for 4 minutes.",
        "Stir the cream cheese and cream together in the pan, and toss it all together.",
    ),
    "Bagt laks med spinat og avocado": (
        "Bake the salmon at 180 °C for 15-18 minutes with lemon and butter.",
        "Sauté the spinach briefly.",
        "Serve with sliced avocado.",
    ),
    "Græsk bøf med tomatsalat": (
        "Mix the meat with oregano and crumbled feta, and shape patties.",
        "Fry the patties for 4 minutes on each side.",
        "Slice the tomato, cucumber and red onion, and toss with oil. Serve with pita.",
    ),
    "Tunsalat med bønner og æg": (
        "Boil the eggs for 8 minutes, and cut them into wedges.",
        "Mix the lettuce, tomato, red onion, beans and tuna.",
        "Toss with oil and lemon, and top with the eggs.",
    ),
    "Nudler med kylling og grønt": (
        "Cook the noodles.",
        "Fry the chicken in strips in oil, and add the garlic and sliced vegetables.",
        "Stir in the noodles and soy sauce, and fry for 2 more minutes.",
    ),
    "Bagte søde kartofler med bønner": (
        "Bake the sweet potatoes at 200 °C for 50 minutes.",
        "Warm the beans and corn with cumin.",
        "Split the sweet potatoes, and fill them with the beans, avocado and crème fraîche.",
    ),
    "Havregrød med bær": (
        "Bring the oats and milk to the boil while stirring, and let it simmer for 3 minutes.",
        "Top with berries and honey.",
    ),
    "Yoghurt med müsli og frugt": (
        "Divide the yoghurt between two bowls.",
        "Top with oats, diced apple and honey.",
    ),
    "Rundstykker med ost og marmelade": (
        "Toast the bread.",
        "Serve with butter, cheese and jam.",
    ),
    "Røræg på rugbrød": (
        "Beat the eggs, and stir them slowly in butter over a low heat.",
        "Serve on rye bread with tomato.",
    ),
    "Æg og bacon med avocado": (
        "Fry the bacon until crisp, and fry the eggs in the fat.",
        "Serve with sliced avocado.",
    ),
    "Skyr med bær": (
        "Divide the skyr between two bowls, and top with berries.",
    ),
    "Rugbrød med pålæg": (
        "Butter the rye bread, and top it with cold cuts and cucumber.",
    ),
    "Rugbrød med æg og rejer": (
        "Boil the eggs for 8 minutes, and slice them.",
        "Top the bread with egg, shrimp and mayonnaise, and squeeze lemon over it.",
    ),
    "Makrelmad med løg": (
        "Put the mackerel on the rye bread.",
        "Top with red onion and lettuce.",
    ),
    "Wraps med kylling": (
        "Fry the chicken in strips.",
        "Divide the chicken, greens and crème fraîche between the tortillas, and roll them up.",
    ),
    "Pastasalat med tun": (
        "Cook the pasta, and let it cool.",
        "Stir in the tuna, corn, diced cucumber and mayonnaise.",
    ),
    "Linsesalat med feta": (
        "Boil the lentils until tender, and let them cool.",
        "Toss with tomato, red onion, feta, oil and lemon.",
    ),
    "Hytteost på rugbrød med tomat": (
        "Spread the cottage cheese on the rye bread.",
        "Top with tomato and black pepper.",
    ),
    "Tomatsuppe med toast": (
        "Sauté the onion, add the tomato, stock and 2 dl of water, and boil for 10 minutes. Blend the soup.",
        "Toast the bread with cheese, and serve with the soup.",
    ),
    "Gulerodsstave med hummus": (
        "Cut the carrots into sticks, and serve with hummus.",
    ),
    "Æbler med nødder": (
        "Cut the apples into wedges, and serve with nuts.",
    ),
    "Popcorn": (
        "Heat the oil in a pot, add the corn, and put a lid on. Shake until it stops popping.",
    ),
    "Nachos med ost og salsa": (
        "Spread the chips on a baking tray, and sprinkle with cheese.",
        "Bake at 200 °C for 5 minutes, and serve with salsa.",
    ),
    "Mørk chokolade og nødder": (
        "Break the chocolate into pieces, and serve with nuts.",
    ),
    "Kiks med ost og frugt": (
        "Slice the cheese and apple, and arrange on crackers.",
    ),
    "Hjemmelavet iskaffe": (
        "Brew strong coffee, and let it cool.",
        "Serve over ice with milk and sugar to taste.",
    ),
    "Bærsmoothie": (
        "Blend all the ingredients into a smooth drink.",
    ),
    "Danskvand med citron": (
        "Slice the lemon, and serve with ice-cold sparkling water.",
    ),
    "Appelsinjuice til morgenbordet": (
        "Serve chilled.",
    ),
    "Et glas rødvin til maden": (
        "Serve at 16-18 degrees.",
    ),
}
